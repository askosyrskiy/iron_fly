"""Current config for the next decision.

On the public desk, EM% and the 0.40-put width come from the baked replay tape.
Regime, wings, and the extra stop come from the saved desk config.
The live copy is in memory. A restart reads the last saved row.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from iron_fly.config import Settings
from iron_fly.db import connect
from iron_fly.desk import (
    OWNER_PLAYER,
    _clock,
    _ensure_state,
    _pid,
    _pick_fired,
    _regime_latch,
    _save_pick_fired,
    _save_regime_latch,
    _saved_spec,
    bind_player,
    closed_pause,
    load_book,
    note_em_floor,
    quotes_session,
    reset_player,
    resolve_engine,
    roll_session,
)
from iron_fly.domain.market import DeskMarks
from iron_fly.strategy.config_state import apply_config_state
from iron_fly.strategy.rules import REGIME_MODES, book_clock

log = logging.getLogger("iron_fly.step")

_LOCK = threading.Lock()
_MEMORY: CurrentConfig | None = None
_SEEN: tuple | None = None
_MISS_LOGGED = False


@dataclass(frozen=True)
class CurrentConfig:
    em_pct: float | None
    put40_wings: float | None
    regime: str
    wings: str
    stop: int
    source: str
    updated_at: datetime | None
    em_open: float | None = None
    em_now: float | None = None


def current(conn) -> CurrentConfig:
    """Memory first. The database is the fallback after a restart."""
    global _MEMORY
    with _LOCK:
        if _MEMORY is not None:
            return _MEMORY
    loaded = _read(conn)
    with _LOCK:
        if _MEMORY is None:
            _MEMORY = loaded
        return _MEMORY


def prime(settings: Settings) -> CurrentConfig:
    """Load the saved row before the first tick."""
    global _MEMORY
    conn = connect(settings.db_path)
    token = bind_player(OWNER_PLAYER)
    try:
        _ensure_state(conn)
        loaded = _read(conn)
        conn.commit()
    finally:
        reset_player(token)
        conn.close()
    with _LOCK:
        _MEMORY = loaded
    return loaded


def tick(settings: Settings, *, now: datetime | None = None) -> CurrentConfig:
    """One refresh. A market miss keeps the EM% and 0.40 wings already held."""
    conn = connect(settings.db_path)
    token = bind_player(OWNER_PLAYER)
    try:
        config = _refresh(conn, settings, now)
        conn.commit()
        return config
    finally:
        reset_player(token)
        conn.close()


def serve(settings: Settings, stop: threading.Event, interval: float = 1.0) -> None:
    """Refresh until `stop` is set. One bad tick does not end the loop."""
    waiting = False
    polling = False
    while not stop.is_set():
        # A short interval is a test. The live loop sleeps through a closed market.
        if interval >= 1.0 and not quotes_session():
            if not waiting:
                log.info("Config feed is waiting for the market. EM% and 0.40 wings update weekdays from 9:29 ET.")
                waiting = True
                polling = False
            stop.wait(closed_pause())
            continue
        started = time.monotonic()
        try:
            tick(settings)
            if quotes_session():
                waiting = False
                if not polling:
                    log.info("Config feed updating from the replay desk.")
                    polling = True
            elif not waiting:
                log.info("Config feed is waiting for the market. EM% and 0.40 wings update weekdays from 9:29 ET.")
                waiting = True
                polling = False
        except Exception:
            log.exception("config feed tick failed")
        remaining = interval - (time.monotonic() - started)
        if remaining > 0:
            stop.wait(remaining)


def clear() -> None:
    """Drop the in-memory copy. Tests use this between cases."""
    global _MEMORY, _SEEN, _MISS_LOGGED
    with _LOCK:
        _MEMORY = None
        _SEEN = None
        _MISS_LOGGED = False


def _refresh(conn, settings: Settings, now: datetime | None) -> CurrentConfig:
    global _MISS_LOGGED
    clock = _clock(now)
    roll_session(conn, clock)
    previous = current(conn)
    conn.commit()
    market = _market(settings, conn, clock) if quotes_session(clock) else None
    em_pct = previous.em_pct
    put40 = previous.put40_wings
    em_open = previous.em_open
    em_now = previous.em_now
    source = previous.source
    if market is None:
        if quotes_session(clock) and not _MISS_LOGGED:
            log.warning("Config feed has no live EM% (replay desk)")
            _MISS_LOGGED = True
    else:
        _MISS_LOGGED = False
        if market.em_pct is not None:
            em_pct = market.em_pct
        if market.put_40_width is not None:
            put40 = market.put_40_width
        if market.em_open is not None:
            em_open = market.em_open
        if market.em_now is not None:
            em_now = market.em_now
        source = market.source or source
    _release_prior_day_wings(conn, previous, clock)
    pct_low, em_low = note_em_floor(conn, clock.date().isoformat(), em_pct, em_now)
    regime, wings, stop, use_config, yaml_on = _desk(
        conn, clock, em_pct, put40, em_now=em_now, em_pct_low=pct_low, em_low=em_low
    )
    config = CurrentConfig(
        em_pct=em_pct,
        put40_wings=put40,
        regime=regime,
        wings=wings,
        stop=stop,
        source=source,
        updated_at=clock,
        em_open=em_open,
        em_now=em_now,
    )
    _write(conn, config)
    _remember(config)
    if not quotes_session(clock):
        return config
    if market is None:
        market_from = "kept"
    else:
        market_from = market.source or source
    log.info(
        "config %s use_config=%s yaml=%s em%%=%s open=%s em=%s 0.40=%s regime=%s wings=%s stop=+%s source=%s market=%s",
        clock.strftime("%H:%M:%S"),
        int(use_config),
        "yes" if yaml_on else "no",
        _fmt(em_pct, 1),
        _fmt(em_open, 1),
        _fmt(em_now, 1),
        _fmt(put40, 0),
        regime,
        wings,
        stop,
        source,
        market_from,
    )
    return config


def _market(settings: Settings, conn, clock: datetime) -> DeskMarks | None:
    """Public desk has no live EM feed; replay supplies EM from the baked tape."""
    return None


def _release_prior_day_wings(conn, previous: CurrentConfig, clock: datetime) -> None:
    """Drop a narrow width saved on an earlier day so today can start at the YAML width."""
    row = conn.execute(
        "SELECT em_open_day FROM desk_state WHERE player_id = ?",
        (_pid(),),
    ).fetchone()
    stale_open = bool(row and row["em_open_day"] and row["em_open_day"] != clock.date().isoformat())
    stale_memory = previous.updated_at is not None and previous.updated_at.date() != clock.date()
    if not stale_open and not stale_memory:
        return
    conn.execute("UPDATE desk_state SET eff_wings = NULL WHERE player_id = ?", (_pid(),))


def _note_policy(conn, clock: datetime, regime: str, stop: int, wings: str) -> None:
    from iron_fly.orders import note_policy
    from iron_fly.strategy.rules import wings_for_clock

    shown = wings if str(wings) != "auto" else str(wings_for_clock(book_clock(clock)))
    note_policy(conn, clock, regime, int(stop), shown)


def _desk(
    conn,
    clock: datetime,
    em_pct: float | None,
    put40: float | None,
    *,
    em_now: float | None = None,
    em_pct_low: float | None = None,
    em_low: float | None = None,
) -> tuple[str, str, int, bool, bool]:
    wall = book_clock(clock)
    engine = resolve_engine(
        conn,
        wall,
        em_pct=em_pct,
        em_now=em_now,
        em_pct_low=em_pct_low,
        em_low=em_low,
    )
    regime = str(engine["regime"])
    wings = str(engine["wings"])
    stop = int(engine["stop"])
    spec = _saved_spec(conn) if engine["use_config"] else None
    if spec is None:
        regime, wings, stop = _clean(regime, wings, stop)
        _note_policy(conn, clock, regime, stop, wings)
        return regime, wings, stop, bool(engine["use_config"]), False
    state = apply_config_state(
        spec,
        load_book(conn),
        spot=0.0,
        em_pct=em_pct,
        wall=wall,
        wing_width=put40,
        wings=spec.wings,
        em_now=em_now,
        latest_wings=str(engine["wings"]) if spec.desk == "auto" else None,
        em_pct_low=em_pct_low,
        em_low=em_low,
        regime_latch=_regime_latch(conn),
        pick_fired=_pick_fired(conn),
    )
    _save_regime_latch(conn, state.regime_latch)
    _save_pick_fired(conn, state.pick_fired)
    regime, wings, stop = _clean(state.regime, state.wings, state.stop)
    _note_policy(conn, clock, regime, stop, wings)
    return regime, wings, stop, True, True


def _clean(regime: str, wings: str, stop: int) -> tuple[str, str, int]:
    from iron_fly.strategy.rules import valid_stop_pad

    if regime not in REGIME_MODES:
        regime = "s1"
    if not valid_stop_pad(stop):
        stop = 0
    return regime, wings, stop


def _read(conn) -> CurrentConfig:
    row = conn.execute("SELECT * FROM desk_state WHERE player_id = ?", (_pid(),)).fetchone()
    if row is None:
        return CurrentConfig(None, None, "s1", "50", 0, "database", None)
    regime = row["eff_regime"] or row["regime"] or "s1"
    wings = row["eff_wings"] or str(row["wings"] if row["wings"] is not None else 50)
    stop = row["eff_stop"] if row["eff_stop"] is not None else int(row["stop_pad"] or 0)
    regime, wings, stop = _clean(str(regime), str(wings), int(stop))
    today = _clock(None).date().isoformat()
    same_day = row["em_open_day"] == today
    if row["em_open_day"] and not same_day:
        wings = str(row["wings"] if row["wings"] is not None else 50)
    return CurrentConfig(
        _float(row["em_pct"]),
        _float(row["put40_wings"]),
        regime,
        wings,
        stop,
        "database",
        None,
        em_open=_float(row["em_open"]) if same_day else None,
        em_now=_float(row["em_now"]) if same_day else None,
    )


def _write(conn, config: CurrentConfig) -> None:
    conn.execute(
        """
        UPDATE desk_state
        SET em_pct = ?, put40_wings = ?, em_open = ?, em_open_day = ?, em_now = ?,
            eff_regime = ?, eff_stop = ?, eff_wings = ?
        WHERE player_id = ?
        """,
        (
            config.em_pct,
            config.put40_wings,
            config.em_open,
            None if config.em_open is None or config.updated_at is None else config.updated_at.date().isoformat(),
            config.em_now,
            config.regime,
            config.stop,
            config.wings,
            _pid(),
        ),
    )


def _remember(config: CurrentConfig) -> None:
    global _MEMORY, _SEEN
    signature = (
        config.em_pct,
        config.put40_wings,
        config.em_open,
        config.em_now,
        config.regime,
        config.wings,
        config.stop,
        config.source,
    )
    with _LOCK:
        _MEMORY = config
        changed = signature != _SEEN
        _SEEN = signature
    if changed:
        em = "—" if config.em_pct is None else f"{config.em_pct:.1f}"
        width = "—" if config.put40_wings is None else f"{config.put40_wings:.0f}"
        log.info(
            "Config EM%% %s · 0.40 wings %s · %s · wings %s · stop +%s · %s",
            em,
            width,
            config.regime,
            config.wings,
            config.stop,
            config.source,
        )


def _fmt(value: float | None, digits: int) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def _float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
