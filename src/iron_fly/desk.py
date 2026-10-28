"""Wire Dale OG to the quote source for this container. Live does not send."""

from __future__ import annotations

import contextvars
import json
import logging
import sqlite3
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from iron_fly.config import MODES, Settings
from iron_fly.domain.market import DeskMarks, combo_fill, combo_mark, combo_quote, leg_book, put40_width, straddle_mid
from iron_fly.domain.models import Intent, IntentKind, Quote
from iron_fly.orders import (
    consume_wing_lots,
    dead_long_banner,
    dead_reason,
    dead_wings,
    _fly_legs,
    forget_policy,
    leftover_long_note,
    note_event,
    note_policy,
    leftover_long_panel,
    leftover_long_rows,
    leftovers_sell_allowed,
    live_wing_bids,
    park_lots,
    parked_cash_points,
    parked_long_value,
    priced_short_conflicts,
    clear_open_conflict,
    load_open_conflict,
    open_book_hold,
    fly_spans,
    record_open_blocked,
    widen_blocked_open,
    release_longs,
    reusable_wing_lots,
    sell_ready_leftovers,
    settle_parked_longs,
    short_mark,
)
from iron_fly.quotes.replay import (
    baked_days,
    chain_memory,
    has_chain_memory,
    read_cached_day,
    release_cached_day,
)
from iron_fly.quotes.symbols import spxw_symbol
from iron_fly.strategy.book import FlyBook
from iron_fly.strategy.config_state import ConfigState, apply_config_state, pick_is_on, slippage_now
from iron_fly.strategy.dale_og import TRADE_STOP, DaleOg
from iron_fly.strategy.rules import (
    REGIME_LABELS,
    REGIME_MODES,
    book_clock,
    drop_levels,
    fly_expiration_pnl,
    round_to_5,
    wings_for_clock,
)
from iron_fly.strategy.spec import DEFAULT_SPEC, PRIOR_CLASSROOM_PICKS, DeskSpec, parse_spec

_ET = ZoneInfo("America/New_York")
_SPX_LOCK = threading.Lock()
log = logging.getLogger("iron_fly.step")
_OPEN = time(9, 30)
_OPEN_AVG_END = time(9, 30, 30)
_OPEN_LATCH_END = time(9, 35)
_QUOTE_START = time(9, 29)
_QUOTE_STOP = time(16, 10)
_CASH_AT = time(16, 0, 1)
# Official cash close: GET /v1/spx last at/after this clock.
_CASH_FETCH_AT = time(16, 5)
_CASH_CLOSE: dict[str, float] = {}
_CASH_TRIED: dict[str, datetime] = {}
_CASH_HTTP_DONE: set[str] = set()
LOCAL_FILLS = frozenset()
# Public desk fills from the baked replay tape at the combo mid.
BOOK_FILLS = frozenset()
CONFIG_MODES = frozenset(MODES)
_BOOK_LOCK = threading.Lock()
_PREV_PRINT: dict[str, tuple[float, str]] = {}
_ROLL_LOCK = threading.Lock()
_SESSION_DAY = ""
BACKTEST_MODES = frozenset(MODES)
OWNER_PLAYER = "owner"
_player: contextvars.ContextVar[str] = contextvars.ContextVar("iron_fly_player", default=OWNER_PLAYER)
FLY_WIDTHS = (50, 40, 30, 25, 20, 15, 10)
# Penny placeholder used when combo mid was missing; refuse that STO so backtest
# does not open/close-churn at K ± 0.01.
MIN_OPEN_CREDIT = 0.01
_CHAIN_CACHE: dict[tuple[str, int], object] = {}
_SPX_AT: dict[str, dict[int, dict]] = {}
_RESIDENT: list[str] = []
_RESIDENT_MAX = 2
_UNLOADABLE: set[str] = set()
_CHAIN_TRIED: set[str] = set()


def current_player() -> str:
    return _player.get()


def bind_player(player_id: str) -> contextvars.Token[str]:
    return _player.set(player_id)


def reset_player(token: contextvars.Token[str]) -> None:
    _player.reset(token)


def _pid() -> str:
    return current_player()


def load_backtest(settings: Settings, day: date | None = None) -> tuple[DeskMarks, list[str], str]:
    """Baked replay tape for one session. The newest stored day is the default."""
    days = baked_days()
    if not days:
        return DeskMarks(error="No baked sessions", source="replay"), [], ""
    chosen = day.isoformat() if day is not None and day.isoformat() in days else days[-1]
    return load_day_tape(settings, date.fromisoformat(chosen)), days, chosen


def load_day_tape(settings: Settings, day: date) -> DeskMarks:
    """Cached baked session tape from data_dir/replay or shipped replay."""
    root = settings.data_dir / "replay"
    cached = read_cached_day(root, day)
    if cached is not None and cached.tape and not cached.error:
        _remember_spx(day, cached.tape)
        _keep_resident(day)
        return cached
    return DeskMarks(error=f"No baked tape for {day.isoformat()}", source="replay")


def _remember_spx(day: date, tape: list) -> None:
    key = day.isoformat()
    if key in _SPX_AT or not tape:
        return
    with _SPX_LOCK:
        if key in _SPX_AT or not tape:
            return
        last = int(tape[-1]["time"])
        _SPX_AT[key] = {int(row["time"]): row for row in tape_seconds(tape, None, last)}
        log.info("preloaded %s spx seconds=%s", key, len(_SPX_AT[key]))


def warm_shared_replay() -> None:
    """Remember shipped session names. Chain books load when a day is opened."""
    from iron_fly.quotes.replay import _shipped_replay_dirs, note_baked_days

    names: list[str] = []
    seen: set[str] = set()
    for directory in _shipped_replay_dirs():
        for path in sorted(directory.glob("????-??-??.json")):
            if path.stem in seen:
                continue
            try:
                date.fromisoformat(path.stem)
            except ValueError:
                continue
            seen.add(path.stem)
            names.append(path.stem)
    note_baked_days(sorted(names))
    log.info("shared replay days=%s", len(names))


def warm_default_day() -> None:
    """Parse the newest shipped session after the process is already listening."""
    from iron_fly.quotes.replay import _shipped_replay_dirs, read_cached_day

    directory: Path | None = None
    day: date | None = None
    for folder in _shipped_replay_dirs():
        for path in sorted(folder.glob("????-??-??.json")):
            try:
                found = date.fromisoformat(path.stem)
            except ValueError:
                continue
            directory, day = folder, found
    if directory is None or day is None:
        return
    marks = read_cached_day(directory, day)
    if marks is None or not marks.tape or marks.error:
        return
    _remember_spx(day, marks.tape)
    _keep_resident(day)
    log.info("shared replay ready %s", day.isoformat())


def _keep_resident(day: date) -> None:
    """Keep the two most recently opened sessions. Micro has 1 GB."""
    key = day.isoformat()
    with _SPX_LOCK:
        if key in _RESIDENT:
            _RESIDENT.remove(key)
        _UNLOADABLE.discard(key)
        _RESIDENT.append(key)
        while len(_RESIDENT) > _RESIDENT_MAX:
            drop = _RESIDENT.pop(0)
            _SPX_AT.pop(drop, None)
            release_cached_day(drop)
            log.info("released replay %s", drop)


def _spx_slice(day: date, after: int | None, until: int) -> list[dict] | None:
    index = _SPX_AT.get(day.isoformat())
    if not index:
        return None
    start = after + 1 if after is not None else min(index)
    if start > until:
        return []
    return [index[stamp] for stamp in range(start, until + 1) if stamp in index]


def quotes_session(when: datetime | None = None) -> bool:
    """Weekday quote window, 9:29 ET inclusive through 16:10 ET exclusive."""
    clock = _clock(when)
    if clock.weekday() >= 5:
        return False
    wall = clock.timetz().replace(tzinfo=None)
    return _QUOTE_START <= wall < _QUOTE_STOP


def closed_pause(when: datetime | None = None) -> float:
    """Seconds to sleep while the quote window is shut. At most one minute."""
    clock = _clock(when)
    if quotes_session(clock):
        return 0.0
    nxt = datetime.combine(clock.date(), _QUOTE_START, tzinfo=_ET)
    while nxt <= clock or nxt.weekday() >= 5:
        nxt = datetime.combine(nxt.date() + timedelta(days=1), _QUOTE_START, tzinfo=_ET)
    return max(1.0, min(60.0, (nxt - clock).total_seconds()))


def load_marks(
    settings: Settings, conn: sqlite3.Connection, now: datetime | None = None, *, force: bool = False
) -> DeskMarks:
    """Public desk uses baked replay only; live quote sources are not available."""
    clock = _clock(now)
    marks = DeskMarks(source="replay", ts=clock, error="Live quotes are off; use baked replay")
    _latch_em(conn, marks, clock)
    return marks


def _sale_rows(sales: list) -> list[dict]:
    rows = []
    for sale in sales or []:
        if not isinstance(sale, dict):
            continue
        right = sale.get("right")
        strike = sale.get("strike")
        bid = sale.get("bid")
        if right is None or strike is None or bid is None:
            continue
        rows.append({"right": str(right), "strike": float(strike), "bid": float(bid)})
    return rows


def _sales_from_row(row) -> list[dict]:
    keys = row.keys() if hasattr(row, "keys") else []
    if "sales_json" not in keys:
        return []
    try:
        parsed = json.loads(row["sales_json"] or "[]")
    except json.JSONDecodeError:
        return []
    return _sale_rows(parsed if isinstance(parsed, list) else [])


def _remember_sales(fly, lots: list[dict]) -> None:
    rows = _sale_rows(lots)
    if not rows:
        return
    current = getattr(fly, "sales", None)
    if current is None:
        fly.sales = rows
        return
    current.extend(rows)


def _attach_call_wings(fly, call_wings: int | None) -> None:
    """Remember a call wing that is wider or narrower than the put."""
    if fly is None or call_wings is None or int(call_wings) == int(fly.wings):
        return
    fly.call_wings = int(call_wings)


def _stored_call_wings(fly) -> int | None:
    extra = getattr(fly, "call_wings", None)
    if extra is None or int(extra) == int(fly.wings):
        return None
    return int(extra)


def _active_fly(book: FlyBook, strike: float):
    return next((fly for fly in book.active() if fly.strike == float(strike)), None)


def load_book(conn: sqlite3.Connection) -> FlyBook:
    book = FlyBook()
    state = conn.execute("SELECT * FROM desk_state WHERE player_id = ?", (_pid(),)).fetchone()
    if state is not None:
        if state["regime"] in REGIME_MODES:
            book.regime = state["regime"]
        book.stop_pad = int(state["stop_pad"])
    rows = conn.execute("SELECT * FROM fly WHERE player_id = ? ORDER BY id", (_pid(),)).fetchall()
    for row in rows:
        opened = datetime.fromisoformat(row["opened_at"])
        call_wings = row["call_wings"] if "call_wings" in row.keys() else None
        book.open_fly(
            float(row["strike"]),
            float(row["credit"]),
            int(row["wings"]),
            opened,
            origin=str(row["origin"] or ""),
        )
        _attach_call_wings(book.flies[-1], None if call_wings is None else int(call_wings))
        if row["status"] == "closed" and row["close_debit"] is not None:
            closed = datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else opened
            book.close_fly(float(row["strike"]), float(row["close_debit"]), closed)
        book.flies[-1].sales = _sales_from_row(row)
    return book


def save_book(conn: sqlite3.Connection, book: FlyBook) -> None:
    pid = _pid()
    conn.execute("DELETE FROM fly WHERE player_id = ?", (pid,))
    for fly in book.flies:
        conn.execute(
            """
            INSERT INTO fly (
                player_id, strike, wings, credit, opened_at, status, close_debit, closed_at, origin, sales_json,
                call_wings
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pid,
                fly.strike,
                fly.wings,
                fly.credit,
                fly.opened_at.isoformat(),
                fly.status,
                fly.close_debit,
                fly.closed_at.isoformat() if fly.closed_at else None,
                fly.origin,
                json.dumps(_sale_rows(getattr(fly, "sales", []))),
                _stored_call_wings(fly),
            ),
        )
    conn.execute(
        """
        INSERT INTO desk_state (player_id, regime, stop_pad)
        VALUES (?, ?, ?)
        ON CONFLICT(player_id) DO UPDATE SET regime = excluded.regime, stop_pad = excluded.stop_pad
        """,
        (pid, book.regime, int(book.stop_pad)),
    )


def _floor(held: float | None, seen: float | None) -> float | None:
    if seen is None:
        return held
    if held is None:
        return float(seen)
    return min(float(held), float(seen))


def _tape_floor(tape: list, until: int | None) -> tuple[float | None, float | None]:
    """Lowest EM% and EM on prints at or before `until`. None walks nothing."""
    if until is None:
        return None, None
    pct = em = None
    for point in tape:
        if int(point["time"]) > until:
            continue
        pct = _floor(pct, _point_num(point.get("pct")))
        em = _floor(em, _point_num(point.get("em")))
    return pct, em


def note_em_floor(
    conn: sqlite3.Connection,
    day: str,
    em_pct: float | None,
    em_now: float | None,
) -> tuple[float | None, float | None]:
    """Remember today's lowest EM% and EM. A new day starts the low over."""
    _ensure_state(conn)
    row = conn.execute(
        "SELECT em_pct_low, em_low, em_low_day FROM desk_state WHERE player_id = ?",
        (_pid(),),
    ).fetchone()
    pct = em = None
    if row is not None and row["em_low_day"] == day:
        pct = None if row["em_pct_low"] is None else float(row["em_pct_low"])
        em = None if row["em_low"] is None else float(row["em_low"])
    pct = _floor(pct, em_pct)
    em = _floor(em, em_now)
    if pct is not None or em is not None:
        conn.execute(
            "UPDATE desk_state SET em_pct_low = ?, em_low = ?, em_low_day = ? WHERE player_id = ?",
            (pct, em, day, _pid()),
        )
    return pct, em


def session_floors(conn: sqlite3.Connection, settings: Settings, marks: DeskMarks) -> tuple[float | None, float | None]:
    """Lows through the playhead. Live also keeps the tick low the tape can miss."""
    until = int(marks.ts.timestamp()) if marks.ts is not None else None
    pct, em = _tape_floor(marks.tape, until)
    pct = _floor(pct, marks.em_pct)
    em = _floor(em, marks.em_now)
    if settings.desk_mode in BACKTEST_MODES:
        return pct, em
    day = _clock(marks.ts).date().isoformat()
    return note_em_floor(conn, day, pct, em)


def sheet(
    conn: sqlite3.Connection, settings: Settings, marks: DeskMarks, now: datetime | None = None, *, brief: bool = False
) -> dict:
    clock = _clock(now or marks.ts)
    if settings.desk_mode not in BACKTEST_MODES:
        roll_session(conn, clock)
    prior_wings = _eff_wings(conn)
    _latch_em(conn, marks, clock)
    pct_low, em_low = session_floors(conn, settings, marks)
    engine = resolve_engine(
        conn,
        book_clock(clock),
        em_pct=marks.em_pct,
        em_now=marks.em_now,
        em_pct_low=pct_low,
        em_low=em_low,
    )
    book = load_book(conn)
    if engine["use_config"]:
        book.regime = str(engine["regime"])
        book.stop_pad = int(engine["stop"])
    if marks.last is not None:
        book.on_quote(Quote(symbol="SPX", last=marks.last, bid=marks.bid, ask=marks.ask, ts=clock))
    spot = marks.last if marks.last is not None else book.spot
    spec = _saved_spec(conn) if engine["use_config"] else None
    if spec is not None and spot is not None:
        wings_arg = spec.wings
        if spec.desk == "auto":
            wings_arg = _starting_wings(prior_wings, spec.wings)
        state = apply_config_state(
            spec,
            book,
            spot=float(spot),
            em_pct=marks.em_pct,
            wall=book_clock(clock),
            wing_width=marks.put_40_width,
            wings=wings_arg,
            em_now=marks.em_now,
            latest_wings=prior_wings or None,
            em_pct_low=pct_low,
            em_low=em_low,
            regime_latch=_regime_latch(conn),
        )
        _save_regime_latch(conn, state.regime_latch)
        _adopt_state(conn, engine, state, book_clock(clock))
    note_policy(conn, clock, str(engine["regime"]), int(engine["stop"]), str(engine["wings"]))
    if spot is None:
        data = {
            "last_price": None,
            "ladder": [],
            "flies": [],
            "max_profit": None,
            "regime": book.regime,
            "stop_pad": book.stop_pad,
            "wings": wings_for_clock(book_clock(clock)),
        }
        if book.flies:
            pinned = book.active_strikes()[-1] if book.active_strikes() else book.flies[-1].strike
            data["flies"] = book.view_model(pinned)["flies"]
    else:
        data = book.live_strip(spot, clock)
    ensure_cash_close(conn, settings, marks, clock)
    _stamp_flies(data, book, marks, clock)
    cash_points = parked_cash_points(conn, clock.date().isoformat())
    parked_value = parked_long_value(
        conn, marks.chain, _cash_close(marks) if book_clock(clock) >= _CASH_AT else None
    )
    data["parked_cash_points"] = cash_points
    data["parked_value_points"] = parked_value
    fly_total = data.get("pnl_mid_total")
    if parked_value is None:
        data["pnl_mid_total"] = None
    elif fly_total is not None or cash_points or parked_value:
        data["pnl_mid_total"] = round((fly_total or 0) + cash_points + parked_value, 4)
    data["em_open"] = marks.em_open
    data["em_now"] = marks.em_now
    data["em_pct"] = marks.em_pct
    data["put_40_width"] = marks.put_40_width
    tradable = settings.desk_mode in BOOK_FILLS or settings.desk_mode in BACKTEST_MODES
    data["run_mode"] = "auto" if engine["auto_open"] or engine["auto_drop"] else "manual"
    data["use_config"] = engine["use_config"]
    data["pref_auto_open"] = engine["pref_auto_open"]
    data["pref_auto_drop"] = engine["pref_auto_drop"]
    data["auto_open"] = engine["auto_open"]
    data["auto_drop"] = engine["auto_drop"]
    data["open_paused"] = engine["open_paused"]
    data["leftover_longs"] = leftover_long_panel(conn, marks.chain)
    data["dead_longs"] = dead_long_banner(data.get("flies") or [])
    data["open_conflict"] = load_open_conflict(conn)
    data["engine_line"] = engine["line"]
    data["has_fly"] = bool(book.flies)
    data["block_open"] = not tradable
    data["block_drop"] = not tradable
    data["orders_readonly"] = data["block_open"] and data["block_drop"]
    data["show_config"] = settings.desk_mode in CONFIG_MODES
    data["spec_yaml"] = ""
    data["spec_summary"] = ""
    if data["show_config"]:
        saved = load_spec_yaml(conn)
        data["spec_yaml"] = saved
        if saved.strip():
            try:
                data["spec_summary"] = parse_spec(saved).summary()
            except ValueError:
                data["spec_summary"] = ""
    data["desk_mode"] = settings.desk_mode
    data["source"] = marks.source
    data["quote_error"] = marks.error
    data["tape"] = [] if brief else (marks.tape or [])
    data["tape_json"] = "[]" if brief else json.dumps(data["tape"])
    data["chain_json"] = "{}" if brief else json.dumps(chain_packet(marks.chain))
    data["regimes"] = list(REGIME_MODES)
    data["wing_choice"] = int(engine["wings"]) if str(engine["wings"]).isdigit() else load_wings(conn)
    data["lines_json"] = json.dumps(line_payload(data))
    _store_ladder(conn, data.get("ladder") or [])
    return data


def _skip_open(
    conn: sqlite3.Connection,
    book: FlyBook,
    clock: datetime,
    strike: float,
    wings: int,
    reason: str,
) -> str:
    record_open_blocked(conn, clock, strike, wings, reason, book.active())
    log.info("step %s fill open %s skipped %s", _clock_text(clock), int(strike), reason)
    return "conflict"


def open_strike(
    conn: sqlite3.Connection,
    settings: Settings,
    marks: DeskMarks,
    strike: float,
    now: datetime | None = None,
    day: date | None = None,
    wings: int | None = None,
    credit: float | None = None,
) -> str | None:
    clock = _clock(now or marks.ts)
    engine = resolve_engine(conn, book_clock(clock))
    book = load_book(conn)
    if settings.desk_mode not in BOOK_FILLS and settings.desk_mode not in BACKTEST_MODES:
        return "live"
    if any(fly.strike == float(strike) for fly in book.active()):
        log.info("step %s fill open %s skipped already open", _clock_text(clock), int(strike))
        return "duplicate"
    width = wings if wings in FLY_WIDTHS else load_wings(conn)
    if wings in FLY_WIDTHS and not engine["use_config"]:
        set_wings(conn, width)
    chain = marks.chain
    if settings.desk_mode in BACKTEST_MODES and day is not None:
        chain = chain_asof(settings, day, clock)
    requested = width
    chosen = widen_blocked_open(book.active(), strike, requested)
    if chosen is None:
        return _skip_open(
            conn, book, clock, strike, requested, open_book_hold(book.active(), strike, requested) or ""
        )
    width, call_w = chosen
    conflict, wing_sales = priced_short_conflicts(conn, chain, strike)
    if conflict:
        return _skip_open(conn, book, clock, strike, requested, conflict)
    reused = reusable_wing_lots(conn, chain, strike, width, call_w)
    if reused is not None:
        rejected, fill = _send_open_shorts(settings, conn, chain, strike)
        if rejected:
            return rejected
        credit = _subtract_open_slippage(conn, clock, _straddle_credit(fill, chain, strike), _SHORT_LEGS)
        if credit is None or float(credit) <= MIN_OPEN_CREDIT:
            log.info(
                "step %s fill open %s skipped no straddle credit for reused longs",
                _clock_text(clock),
                int(strike),
            )
            return "nomid"
        consume_wing_lots(conn, reused)
        _release_blocking_longs(conn, book, wing_sales, _clock_text(clock), clock)
        opened = book.open_fly(strike, round(float(credit), 2), width, clock)
        _attach_call_wings(opened, call_w)
        clear_open_conflict(conn)
        note_event(
            conn,
            clock,
            f"Opened straddle {int(strike)} ({_fly_legs(strike, width, call_w)}) at {float(credit):.2f}",
        )
        save_book(conn, book)
        log.info(
            "step %s fill open %s credit=%.2f wings=%s reused longs",
            _clock_text(clock),
            int(strike),
            credit,
            width,
        )
        return None
    rejected, fill = _send_live(settings, conn, chain, strike, width, closing=False)
    if rejected:
        return rejected
    open_mode, _ = _fill_modes(conn)
    credit = _subtract_open_slippage(
        conn,
        clock,
        _book_package_price(fill, credit, chain, strike, width, side="open", mode=open_mode, call_wings=call_w),
        _FLY_LEGS,
    )
    if credit is None or float(credit) <= MIN_OPEN_CREDIT:
        if (
            settings.desk_mode in BACKTEST_MODES
            or fill is None
            or abs(float(fill)) <= MIN_OPEN_CREDIT
        ):
            log.warning(
                "step %s fill open %s deferred no quotes %s (retry)",
                _clock_text(clock),
                int(strike),
                leg_book(chain, strike, width, call_w),
            )
            return "nomid"
        # Live order already accepted — never leave the blotter empty.
        log.error(
            "step %s fill open %s booked without mid after live fill=%s",
            _clock_text(clock),
            int(strike),
            fill,
        )
        credit = abs(float(fill))
    _release_blocking_longs(conn, book, wing_sales, _clock_text(clock), clock)
    opened = book.open_fly(strike, round(float(credit), 2), width, clock)
    _attach_call_wings(opened, call_w)
    clear_open_conflict(conn)
    note_event(conn, clock, f"Opened fly {int(strike)} ({_fly_legs(strike, width, call_w)}) at {float(credit):.2f}")
    save_book(conn, book)
    log.info(
        "step %s fill open %s credit=%.2f wings=%s",
        _clock_text(clock),
        int(strike),
        credit,
        width,
    )
    return None


_FLY_LEGS = 4
_SHORT_LEGS = 2


def _add_close_slippage(
    conn: sqlite3.Connection, when: datetime, debit: float | None, contracts: int = _FLY_LEGS
) -> float | None:
    """Add config slippage and per-contract fees to a backtest close debit."""
    if debit is None:
        return None
    cost = _active_slippage(conn, when) + _fee_points(conn, contracts)
    if cost <= 0:
        return float(debit)
    return round(float(debit) + cost, 2)


def _subtract_open_slippage(
    conn: sqlite3.Connection, when: datetime, credit: float | None, contracts: int = _FLY_LEGS
) -> float | None:
    """Take config slippage and per-contract fees off a backtest open credit."""
    if credit is None:
        return None
    cost = _active_slippage(conn, when) + _fee_points(conn, contracts)
    if cost <= 0:
        return float(credit)
    slipped = round(float(credit) - cost, 2)
    if slipped <= MIN_OPEN_CREDIT:
        return None
    return slipped


def _fee_points(conn: sqlite3.Connection, contracts: int) -> float:
    """Dollar fee per contract, in option points. $1 on one contract is 0.01."""
    if contracts <= 0:
        return 0.0
    spec = _saved_spec(conn)
    if spec is None:
        return 0.0
    dollars = float(spec.fee_per_contract)
    if dollars <= 0:
        return 0.0
    return contracts * dollars / 100.0


def _active_slippage(conn: sqlite3.Connection, when: datetime) -> float:
    spec = _saved_spec(conn)
    if spec is None:
        return 0.0
    row = conn.execute(
        "SELECT em_pct, em_now FROM desk_state WHERE player_id = ?",
        (_pid(),),
    ).fetchone()
    em_pct = None if row is None or row["em_pct"] is None else float(row["em_pct"])
    em_now = None if row is None or row["em_now"] is None else float(row["em_now"])
    return slippage_now(spec, wall=book_clock(when), em_pct=em_pct, em_now=em_now)


def _book_package_price(
    fill: float | None,
    preferred: float | None,
    chain,
    strike: float,
    width: int,
    *,
    side: str = "open",
    mode: str = "mid",
    call_wings: int | None = None,
) -> float | None:
    """Positive package credit/debit for the blotter. Fills may be signed."""
    if fill is not None:
        value = abs(float(fill))
        if 0 < value <= 1000:
            return value
    if preferred is not None:
        value = abs(float(preferred))
        if 0 < value <= 1000:
            return value
    mark = combo_fill(chain, strike, width, side=side, mode=mode, call_wings=call_wings)
    if mark is not None and 0 < mark <= 1000:
        return float(mark)
    return None


def _fill_modes(conn: sqlite3.Connection) -> tuple[str, str]:
    """Open/close fill modes from the applied desk YAML. Defaults to mid/mid."""
    from iron_fly.strategy.spec import FILL_MODE_DEFAULT

    spec = _saved_spec(conn)
    if spec is None:
        return FILL_MODE_DEFAULT, FILL_MODE_DEFAULT
    return spec.fill_mode_open, spec.fill_mode_close


def close_strike(
    conn: sqlite3.Connection,
    settings: Settings,
    marks: DeskMarks,
    strike: float,
    now: datetime | None = None,
    day: date | None = None,
) -> str | None:
    clock = _clock(now or marks.ts)
    if settings.desk_mode not in BOOK_FILLS and settings.desk_mode not in BACKTEST_MODES:
        return "live"
    book = load_book(conn)
    fly = next((item for item in book.active() if item.strike == strike), None)
    if fly is None:
        return "missing"
    chain = marks.chain
    if settings.desk_mode in BACKTEST_MODES and day is not None:
        chain = chain_asof(settings, day, clock)
    return _close_open_fly(conn, settings, book, fly, chain, clock)


def flatten(
    conn: sqlite3.Connection, settings: Settings, marks: DeskMarks, now: datetime | None = None, day: date | None = None
) -> str | None:
    clock = _clock(now or marks.ts)
    if settings.desk_mode not in BOOK_FILLS and settings.desk_mode not in BACKTEST_MODES:
        return "live"
    pause_auto_open(conn)
    book = load_book(conn)
    chain = marks.chain
    if settings.desk_mode in BACKTEST_MODES and day is not None:
        chain = chain_asof(settings, day, clock)
    for fly in list(book.active()):
        code = _close_open_fly(conn, settings, book, fly, chain, clock)
        if code == "already closing":
            continue
        if code:
            return code
    return None


def pause_auto_open(conn: sqlite3.Connection) -> None:
    """Flatten leaves Auto open off so an empty book does not sell F1 again."""
    _ensure_state(conn)
    conn.execute(
        "UPDATE desk_state SET open_paused = 1, auto_open = 0 WHERE player_id = ?",
        (_pid(),),
    )


def _close_open_fly(conn, settings: Settings, book: FlyBook, fly, chain, clock: datetime) -> str | None:
    """4-leg close, or BTC the shorts and sell each wing that still has a bid."""
    put_w, call_w = fly_spans(fly)
    notes = dead_wings(chain, fly.strike, put_w, call_w)
    if notes:
        debit, contracts, rejected, sold = _buy_shorts_sell_live_wings(
            conn, settings, chain, fly.strike, put_w, _clock_text(clock), clock, call_wings=call_w
        )
        if rejected:
            return rejected
        if debit is None:
            return "nomid"
        _remember_sales(fly, sold)
    else:
        rejected, fill = _send_live(settings, conn, chain, fly.strike, put_w, closing=True)
        if rejected:
            return rejected
        _, close_mode = _fill_modes(conn)
        debit = _book_package_price(
            fill, None, chain, fly.strike, put_w, side="close", mode=close_mode, call_wings=call_w
        )
        if debit is None or float(debit) <= MIN_OPEN_CREDIT:
            if (
                settings.desk_mode in BACKTEST_MODES
                or fill is None
                or abs(float(fill)) <= MIN_OPEN_CREDIT
            ):
                return "nomid"
            log.error(
                "step %s fill drop %s booked without mid after live fill=%s",
                _clock_text(clock),
                int(fly.strike),
                fill,
            )
            debit = abs(float(fill))
        contracts = _FLY_LEGS
    debit = _add_close_slippage(conn, clock, debit, contracts)
    if not notes:
        note_event(conn, clock, f"Closed fly {int(fly.strike)} at {float(debit):.2f}")
    book.close_fly(fly.strike, debit, clock)
    save_book(conn, book)
    return None


def default_spec_yaml() -> str:
    """Web UI default: repo `config/default.yaml`, else the strategy package DEFAULT_SPEC."""
    for path in (
        Path.cwd() / "config" / "default.yaml",
        Path(__file__).resolve().parents[2] / "config" / "default.yaml",
    ):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if text.strip():
            return text
    return DEFAULT_SPEC


def load_spec_yaml(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT spec_yaml FROM desk_state WHERE player_id = ?", (_pid(),)).fetchone()
    text = "" if row is None or row["spec_yaml"] is None else str(row["spec_yaml"])
    if not text.strip() or _stock_spec(text):
        return default_spec_yaml()
    return text


def _stock_spec(text: str) -> bool:
    """True when the saved YAML is the UI default or an older classroom copy."""
    from datetime import time as clock_time

    stock_text = default_spec_yaml()
    if text.strip() in {stock_text.strip(), DEFAULT_SPEC.strip()}:
        return True
    try:
        saved = parse_spec(text)
        stock = parse_spec(stock_text)
    except ValueError:
        return False
    # An explicit delay override is not an old stock configuration to upgrade.
    if saved.close_fill_ms != stock.close_fill_ms:
        return False
    saved_auto = dict(saved.auto)
    for key, value in stock.auto.items():
        saved_auto.setdefault(key, value)
    same_desk = (
        saved.f1_at == stock.f1_at
        and saved.wings == stock.wings
        and saved.regime == stock.regime
        and saved.stop == stock.stop
        and saved.desk == stock.desk
        and saved_auto == stock.auto
    )
    if same_desk and (saved.picks == stock.picks or saved.picks in PRIOR_CLASSROOM_PICKS):
        return True
    # Older nested classroom YAML (10:50 / ±15/20, one clock pick) still upgrades.
    if saved.picks not in PRIOR_CLASSROOM_PICKS:
        return False
    if saved.wings != "50" or saved.stop != 0:
        return False
    if saved.f1_at not in (clock_time(10, 50), clock_time(10, 40)):
        return False
    return saved.regime in ("s3", "ladder")


def prepare_desk(conn: sqlite3.Connection, settings: Settings) -> None:
    """Backtest desk keeps the saved YAML; no auto-sim bootstrap."""
    return


def apply_spec(conn: sqlite3.Connection, spec: DeskSpec) -> None:
    """Persist YAML and base regime/stop. Clear latch so the next frame/step uses the new file."""
    book = load_book(conn)
    book.regime = spec.regime
    book.stop_pad = spec.stop
    save_book(conn, book)
    conn.execute(
        """
        UPDATE desk_state
        SET spec_yaml = ?, applied_at = NULL, regime_latch = NULL,
            eff_wings = NULL, eff_regime = NULL, eff_stop = NULL, eff_f1 = NULL, eff_clock = NULL
        WHERE player_id = ?
        """,
        (spec.yaml, _pid()),
    )


def set_policy(
    conn: sqlite3.Connection,
    regime: str,
    stop_pad: int,
    run_mode: str | None = None,
    wings: int | None = None,
) -> None:
    if load_switches(conn)["use_config"]:
        return
    book = load_book(conn)
    if regime in REGIME_MODES:
        book.regime = regime
    from iron_fly.strategy.rules import valid_stop_pad

    if valid_stop_pad(stop_pad):
        book.stop_pad = stop_pad
    save_book(conn, book)
    if wings in FLY_WIDTHS:
        set_wings(conn, wings)
    if run_mode in ("manual", "auto"):
        conn.execute("UPDATE desk_state SET run_mode = ? WHERE player_id = ?", (run_mode, _pid()))


def load_wings(conn: sqlite3.Connection) -> int:
    _ensure_state(conn)
    row = conn.execute("SELECT wings FROM desk_state WHERE player_id = ?", (_pid(),)).fetchone()
    if row is None or int(row["wings"] or 0) not in FLY_WIDTHS:
        return 50
    return int(row["wings"])


def set_wings(conn: sqlite3.Connection, wings: int) -> None:
    if wings not in FLY_WIDTHS or load_switches(conn)["use_config"]:
        return
    _ensure_state(conn)
    conn.execute("UPDATE desk_state SET wings = ? WHERE player_id = ?", (int(wings), _pid()))


def load_run_mode(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT run_mode FROM desk_state WHERE player_id = ?", (_pid(),)).fetchone()
    if row is None or row["run_mode"] not in ("manual", "auto"):
        return "manual"
    return str(row["run_mode"])


def load_switches(conn: sqlite3.Connection) -> dict[str, bool]:
    _ensure_state(conn)
    row = conn.execute(
        "SELECT use_config, auto_open, auto_drop, open_paused FROM desk_state WHERE player_id = ?",
        (_pid(),),
    ).fetchone()
    return {
        "use_config": bool(row["use_config"]),
        "auto_open": bool(row["auto_open"]),
        "auto_drop": bool(row["auto_drop"]),
        "open_paused": bool(row["open_paused"]),
    }


def set_switches(conn: sqlite3.Connection, use_config: bool, auto_open: bool, auto_drop: bool) -> None:
    """Store the switches. Use Config owns open/drop (prefs stay off); manual uses Auto Open/Drop."""
    _ensure_state(conn)
    resume_open = bool(auto_open)
    if use_config:
        # Pref columns stay off; resolve_engine / engine force auto_open+auto_drop from YAML.
        auto_open = False
        auto_drop = False
    conn.execute(
        "UPDATE desk_state SET use_config = ?, auto_open = ?, auto_drop = ? WHERE player_id = ?",
        (1 if use_config else 0, 1 if auto_open else 0, 1 if auto_drop else 0, _pid()),
    )
    # Use Config clears flatten pause so open_paused cannot block YAML trading.
    if use_config or resume_open:
        conn.execute("UPDATE desk_state SET open_paused = 0 WHERE player_id = ?", (_pid(),))
    if not use_config and not auto_open and not auto_drop:
        conn.execute("UPDATE desk_state SET applied_at = NULL WHERE player_id = ?", (_pid(),))


def resolve_engine(
    conn: sqlite3.Connection,
    wall: time,
    *,
    em_pct: float | None = None,
    em_now: float | None = None,
    em_pct_low: float | None = None,
    em_low: float | None = None,
) -> dict[str, object]:
    """Write the config row the engine follows at `wall` and return it."""
    switches = load_switches(conn)
    spec = _saved_spec(conn)
    book = load_book(conn)
    regime = book.regime if book.regime in REGIME_MODES else "s3"
    from iron_fly.strategy.rules import valid_stop_pad

    stop = book.stop_pad if valid_stop_pad(int(book.stop_pad)) else 0
    wings = str(load_wings(conn))
    f1 = ""
    auto_open = switches["auto_open"]
    auto_drop = switches["auto_drop"]
    # open_paused / Auto Open / Auto Drop only apply in manual trading.
    if not switches["use_config"] and switches["open_paused"]:
        auto_open = False
    if switches["use_config"] and spec is not None:
        regime = spec.regime
        stop = spec.stop
        wings = spec.wings
        f1 = "" if spec.f1_at is None else spec.f1_at.strftime("%H:%M")
        for pick in spec.picks:
            if not pick_is_on(
                pick,
                wall=wall,
                em_pct=em_pct,
                em_now=em_now,
                em_pct_low=em_pct_low,
                em_low=em_low,
            ):
                continue
            if "regime" in pick:
                regime = str(pick["regime"])
            if "stop" in pick:
                stop = int(pick["stop"])
            if "wings" in pick:
                wings = str(pick["wings"])
        # Use Config always runs automatic open/drop from the YAML rules.
        wings = _starting_wings(_eff_wings(conn), wings)
        auto_open = True
        auto_drop = True
    shown = wings if wings != "auto" else str(wings_for_clock(wall))
    if switches["use_config"]:
        prefix = "Config"
    elif auto_open or auto_drop:
        parts = []
        if auto_open:
            parts.append("open")
        if auto_drop:
            parts.append("drop")
        prefix = "Auto " + " · ".join(parts)
    else:
        prefix = "Manual"
    f1_label = f"F1 {f1}" if f1 else "F1 click"
    line = f"{prefix} · {wall.strftime('%H:%M')} · {REGIME_LABELS[regime]} · stop +{stop} · wings {shown} · {f1_label}"
    conn.execute(
        """
        UPDATE desk_state
        SET eff_regime = ?, eff_stop = ?, eff_wings = ?, eff_f1 = ?, eff_clock = ?
        WHERE player_id = ?
        """,
        (regime, stop, shown, f1, wall.strftime("%H:%M"), _pid()),
    )
    return {
        "use_config": switches["use_config"],
        "pref_auto_open": switches["auto_open"],
        "pref_auto_drop": switches["auto_drop"],
        "auto_open": auto_open,
        "auto_drop": auto_drop,
        "open_paused": switches["open_paused"],
        "regime": regime,
        "stop": stop,
        "wings": shown,
        "f1": f1,
        "clock": wall.strftime("%H:%M"),
        "line": line,
    }


def _ensure_state(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO desk_state (player_id, regime, stop_pad, use_config) VALUES (?, 's3', 0, 1) "
        "ON CONFLICT(player_id) DO NOTHING",
        (_pid(),),
    )


def clear_flies(conn: sqlite3.Connection) -> None:
    """Drop every fly and parked long so a backtest can start over."""
    conn.execute("DELETE FROM fly WHERE player_id = ?", (_pid(),))
    conn.execute("DELETE FROM leftover_long WHERE player_id = ?", (_pid(),))
    conn.execute("DELETE FROM leftover_roll WHERE player_id = ?", (_pid(),))
    conn.execute("DELETE FROM desk_event WHERE player_id = ?", (_pid(),))
    conn.execute(
        "DELETE FROM parked_cashflow WHERE player_id = ? AND session_day = "
        "(SELECT book_day FROM desk_state WHERE player_id = ?)", (_pid(), _pid()),
    )
    forget_policy(_pid())
    conn.execute("DELETE FROM replay_pending_close WHERE player_id = ?", (_pid(),))
    conn.execute(
        "UPDATE desk_state SET applied_at = NULL, regime_latch = NULL, open_conflict = NULL WHERE player_id = ?",
        (_pid(),),
    )


def roll_session(conn: sqlite3.Connection, when: datetime | None = None) -> bool:
    """Start a new ET day with an empty blotter. Yesterday's flies stay in history.

    Settings stay: the YAML, Use Config, and the Auto switches. The day's marks,
    wing latch, leftovers, and ladder do not.

    A write already open on `conn` is committed before the day-roll lock. The
    lock is released only after this work commits. Waiting for the lock while
    a write is open pins every other thread on `database is locked`.
    """
    clock = _clock(when)
    today = clock.date().isoformat()
    conn.commit()
    with _ROLL_LOCK:
        global _SESSION_DAY
        try:
            _ensure_state(conn)
            row = conn.execute(
                "SELECT book_day FROM desk_state WHERE player_id = ?",
                (_pid(),),
            ).fetchone()
            current = None if row is None or not row["book_day"] else str(row["book_day"])
            rolled = False
            if current == today:
                _SESSION_DAY = today
            else:
                stale = current is not None or _book_is_stale(conn, today)
                if not stale:
                    conn.execute(
                        "UPDATE desk_state SET book_day = ? WHERE player_id = ?",
                        (today, _pid()),
                    )
                    _SESSION_DAY = today
                else:
                    session_day = current or _book_session_day(conn) or today
                    count = _archive_flies(conn, session_day)
                    _clear_day_state(conn, today)
                    _SESSION_DAY = today
                    _drop_session_memory()
                    log.info("session roll %s -> %s archived %s flies", session_day, today, count)
                    rolled = True
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        return rolled


def _book_is_stale(conn: sqlite3.Connection, today: str) -> bool:
    rows = conn.execute(
        "SELECT opened_at FROM fly WHERE player_id = ?",
        (_pid(),),
    ).fetchall()
    for row in rows:
        opened = datetime.fromisoformat(str(row["opened_at"]))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=_ET)
        if opened.astimezone(_ET).date().isoformat() < today:
            return True
    return False


def _book_session_day(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT opened_at FROM fly WHERE player_id = ? ORDER BY id LIMIT 1",
        (_pid(),),
    ).fetchone()
    if row is None:
        return None
    opened = datetime.fromisoformat(str(row["opened_at"]))
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=_ET)
    return opened.astimezone(_ET).date().isoformat()


def _archive_flies(conn: sqlite3.Connection, session_day: str) -> int:
    pid = _pid()
    close = _load_cash_close(conn, session_day)
    if close is not None:
        settle_parked_longs(conn, session_day, close)
    conn.execute(
        """
        INSERT INTO fly_history (
            player_id, session_day, strike, wings, credit, opened_at, status,
            close_debit, closed_at, origin, pnl
        )
        SELECT player_id, ?, strike, wings, credit, opened_at, status,
               close_debit, closed_at, origin, pnl
        FROM fly
        WHERE player_id = ?
        """,
        (session_day, pid),
    )
    deleted = conn.execute("DELETE FROM fly WHERE player_id = ?", (pid,)).rowcount
    conn.execute("DELETE FROM replay_pending_close WHERE player_id = ?", (pid,))
    conn.execute("DELETE FROM leftover_long WHERE player_id = ?", (pid,))
    conn.execute("DELETE FROM leftover_roll WHERE player_id = ?", (pid,))
    conn.execute("DELETE FROM desk_event WHERE player_id = ?", (pid,))
    forget_policy(pid)
    return int(deleted)


def _clear_day_state(conn: sqlite3.Connection, today: str) -> None:
    conn.execute(
        """
        UPDATE desk_state
        SET book_day = ?, applied_at = NULL,
            em_open = NULL, em_open_day = NULL, em_open_samples = '[]',
            em_now = NULL, em_pct = NULL, put40_wings = NULL,
            em_low = NULL, em_pct_low = NULL, em_low_day = NULL, regime_latch = NULL,
            cash_close = NULL, cash_close_day = NULL,
            eff_wings = NULL, eff_regime = NULL, eff_stop = NULL, eff_f1 = NULL, eff_clock = NULL,
            ladder_json = '[]', step_json = '', open_paused = 0
        WHERE player_id = ?
        """,
        (today, _pid()),
    )


def _drop_session_memory() -> None:
    """Drop the copies the threads keep of yesterday's book, marks, and tape."""
    _CASH_CLOSE.clear()
    _CASH_TRIED.clear()
    _CASH_HTTP_DONE.clear()
    _PREV_PRINT.clear()
    from iron_fly.current_config import clear as clear_config
    from iron_fly.engine import clear as clear_engine
    from iron_fly.fly_monitor import clear as clear_monitor

    clear_config()
    clear_engine()
    clear_monitor()


def bind_book_day(conn: sqlite3.Connection, day: str) -> None:
    """Keep the blotter for this tape day. A different day starts empty."""
    if not day:
        return
    _ensure_state(conn)
    row = conn.execute("SELECT book_day FROM desk_state WHERE player_id = ?", (_pid(),)).fetchone()
    current = None if row is None else row["book_day"]
    if current == day:
        return
    if current:
        conn.execute("DELETE FROM fly WHERE player_id = ?", (_pid(),))
    conn.execute(
        "UPDATE desk_state SET book_day = ?, applied_at = NULL, regime_latch = NULL WHERE player_id = ?",
        (day, _pid()),
    )


def bind_chain(settings: Settings, marks: DeskMarks, day: date, when: datetime) -> None:
    """Attach the as-of chain so open flies can be marked to the playhead."""
    marks.chain = chain_asof(settings, day, when)


def seek_tape(marks: DeskMarks, at: int) -> None:
    """Point the sheet at the playhead so Watch matches the paused clock."""
    tape = marks.tape or []
    if not tape:
        return
    point = tape[0]
    for item in tape:
        if int(item["time"]) <= at:
            point = item
        else:
            break
    marks.last = float(point["value"])
    marks.ts = datetime.fromtimestamp(int(point["time"]), tz=_ET)
    if point.get("em") is not None:
        marks.em_now = float(point["em"])
    if point.get("pct") is not None:
        marks.em_pct = float(point["pct"])
    if point.get("wings") is not None:
        marks.put_40_width = float(point["wings"])


def line_payload(model: dict) -> dict:
    """Chart marks already priced. The page only places them on the tape."""
    pad = float(model.get("stop_pad") or 0)
    flies = []
    for row in model.get("flies") or []:
        if row.get("status") != "active" or row.get("strike") is None or row.get("sto") is None:
            continue
        lo, hi = drop_levels(float(row["strike"]), float(row["sto"]), pad)
        flies.append(
            {
                "strike": row["strike"],
                "open": row.get("time_open") or "",
                "hi": hi,
                "lo": lo,
            }
        )
    adds = []
    if flies:
        for row in model.get("ladder") or []:
            if row.get("kind") != "add" or not row.get("enabled") or row.get("watch") is None:
                continue
            adds.append(row["watch"])
    return {"flies": flies, "adds": adds}


def _store_ladder(conn: sqlite3.Connection, ladder: list) -> None:
    """Keep the latest server-calculated add and drop rows."""
    rows = [
        {
            "watch": row.get("watch"),
            "kind": row.get("kind"),
            "strike": row.get("strike"),
            "var": row.get("var"),
            "enabled": bool(row.get("enabled")),
        }
        for row in ladder
    ]
    _ensure_state(conn)
    conn.execute("UPDATE desk_state SET ladder_json = ? WHERE player_id = ?", (json.dumps(rows), _pid()))


def chain_packet(chain) -> dict:
    """Strike grid plus combo quotes, so the open-fly box can step without another fetch."""
    strikes = []
    quotes: dict[str, dict[str, float]] = {}
    if chain is not None:
        for row in chain.strikes:
            strikes.append(
                {
                    "k": row.strike,
                    "cb": row.call_bid,
                    "ca": row.call_ask,
                    "pb": row.put_bid,
                    "pa": row.put_ask,
                }
            )
            for width in FLY_WIDTHS:
                quote = combo_quote(chain, row.strike, width)
                if quote is None:
                    continue
                bid, mid, ask = quote
                quotes[f"{int(row.strike)}:{width}"] = {"bid": bid, "mid": mid, "ask": ask}
    return {"strikes": strikes, "quotes": quotes}


def chain_asof(settings: Settings, day: date, when: datetime):
    preloaded = chain_memory(day, when)
    if preloaded is not None:
        return preloaded
    if not has_chain_memory(day):
        if day.isoformat() not in _UNLOADABLE:
            load_day_tape(settings, day)
            if not has_chain_memory(day):
                _UNLOADABLE.add(day.isoformat())
        return chain_memory(day, when)
    return None


def _clock_text(when: datetime) -> str:
    return when.astimezone(_ET).strftime("%H:%M:%S")


def _fly_trace(book: FlyBook) -> str:
    flies = book.active()
    if not flies:
        return "none"
    return " ".join(f"{int(fly.strike)} credit={fly.credit:.2f} wings={fly.wings}" for fly in flies)


def _watch_trace(rows: list) -> str:
    if not rows:
        return "none"
    parts = []
    for row in rows:
        strike = "" if row.get("strike") is None else str(int(row["strike"]))
        parts.append(f"{float(row['watch']):.1f} {row.get('kind')} {strike} {float(row.get('var') or 0):+.2f}")
    return " | ".join(parts)


def _config_trace(spec: DeskSpec | None, book: FlyBook, width: str, point: dict) -> str:
    regime = REGIME_LABELS.get(book.regime, book.regime)
    base = f"regime={regime} stop=+{book.stop_pad} wings={width}"
    if spec is None or "cookies_below" not in spec.auto:
        return base + " cookies=off"
    limit = float(spec.auto["cookies_below"])
    need = int(spec.auto.get("cookies_min_flies") or 0)
    pct = point.get("pct")
    open_n = len(book.active())
    if pct is None:
        rule = "no-em"
    elif float(pct) < limit and open_n >= need:
        rule = "met"
    else:
        rule = "waiting"
    return f"{base} cookies_below={limit:g} em%={pct} open={open_n} min={need} rule={rule}"


def tape_seconds(points: list, after: int | None, until: int) -> list[dict]:
    """One print per second in (after, until]. Gaps repeat the previous SPX and EM."""
    if after is not None and until <= after:
        return []
    start = after + 1 if after is not None else None
    rows: list[dict] = []
    prev: dict | None = None
    for point in points:
        stamp = int(point["time"])
        if prev is not None:
            begin = int(prev["time"]) + 1
            end = min(stamp, until + 1)
            if start is not None:
                begin = max(begin, start)
            for tick in range(begin, end):
                rows.append(_carry_second(prev, tick))
        if stamp > until:
            return rows
        if (start is None or stamp >= start) and stamp <= until:
            rows.append(point)
        prev = point
    if prev is not None and int(prev["time"]) < until:
        begin = int(prev["time"]) + 1
        if start is not None:
            begin = max(begin, start)
        for tick in range(begin, until + 1):
            rows.append(_carry_second(prev, tick))
    return rows


def _carry_second(point: dict, stamp: int) -> dict:
    carried = {"time": stamp, "value": point["value"]}
    for key in ("em", "pct", "wings"):
        if point.get(key) is not None:
            carried[key] = point[key]
    return carried


def _regime_latch(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT regime_latch FROM desk_state WHERE player_id = ?", (_pid(),)).fetchone()
    if row is None or not row["regime_latch"]:
        return None
    return str(row["regime_latch"])


def _save_regime_latch(conn: sqlite3.Connection, name: str | None) -> None:
    conn.execute("UPDATE desk_state SET regime_latch = ? WHERE player_id = ?", (name or None, _pid()))


def _eff_wings(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT eff_wings FROM desk_state WHERE player_id = ?", (_pid(),)).fetchone()
    if row is None or not row["eff_wings"]:
        return ""
    return str(row["eff_wings"])


def _starting_wings(prior: str, spec_wings: str) -> str:
    """Keep a narrower width already latched. A fresh spec width is the ceiling."""
    try:
        old = int(prior)
        cap = int(spec_wings)
    except (TypeError, ValueError):
        return spec_wings
    if old > 0 and old < cap:
        return str(old)
    return spec_wings


def _adopt_state(conn: sqlite3.Connection, engine: dict, state: ConfigState, wall: time) -> None:
    engine["regime"] = state.regime
    engine["stop"] = state.stop
    engine["wings"] = state.wings
    engine["f1"] = "" if state.f1_at is None else state.f1_at.strftime("%H:%M")
    prefix = "Config" if engine.get("use_config") else "Manual"
    f1_label = f"F1 {engine['f1']}" if engine["f1"] else "F1 click"
    shown = state.wings if state.wings != "auto" else str(wings_for_clock(wall))
    engine["line"] = (
        f"{prefix} · {wall.strftime('%H:%M')} · {REGIME_LABELS[state.regime]} · "
        f"stop +{state.stop} · wings {shown} · {f1_label}"
    )
    conn.execute(
        """
        UPDATE desk_state
        SET eff_regime = ?, eff_stop = ?, eff_wings = ?, eff_f1 = ?, eff_clock = ?
        WHERE player_id = ?
        """,
        (state.regime, state.stop, shown, engine["f1"], wall.strftime("%H:%M"), _pid()),
    )


def replay_until(conn: sqlite3.Connection, settings: Settings, day: date, until: datetime) -> datetime:
    """Advance the saved book from the last applied print to `until`.

    Auto Open / Auto Drop keep the flies already in the database and only
    act on prints after the playhead where those switches were turned on.
    Use Config replays the day from the YAML when the book is still empty.
    """
    prior_wings = _eff_wings(conn)
    engine = resolve_engine(conn, book_clock(until))
    log.info(
        "step %s config-row use_config=%s auto_open=%s auto_drop=%s %s",
        _clock_text(until),
        int(bool(engine["use_config"])),
        int(bool(engine["auto_open"])),
        int(bool(engine["auto_drop"])),
        engine["line"],
    )
    if not engine["auto_open"] and not engine["auto_drop"]:
        log.info("step %s stop auto open and auto drop are off", _clock_text(until))
        return until
    applied = _applied_at(conn)
    saved = load_book(conn)
    rebuild = bool(engine["use_config"] and applied is None and not saved.flies)
    if applied is None and not rebuild:
        _set_applied(conn, until)
        log.info("step %s stamp applied_at and keep %s open flies", _clock_text(until), len(saved.active()))
        return until
    if applied is not None and until <= applied:
        log.info("step %s stop already applied through %s", _clock_text(until), _clock_text(applied))
        return until
    marks = load_day_tape(settings, day)
    saved_spec = _saved_spec(conn)
    # Fill pricing applies to Auto Drop even when YAML strategy rules are off.
    delay_ms = saved_spec.close_fill_ms if saved_spec is not None else 0
    spec = saved_spec if engine["use_config"] else None
    # YAML is fixed for this walk. Parsing it again on every leftover second dominated the backtest.
    leftover_rules = spec.auto if spec is not None else _leftover_auto(conn)
    book = FlyBook() if rebuild else saved
    if rebuild and spec is not None:
        book.regime = spec.regime
        book.stop_pad = spec.stop
    strategy = DaleOg(book)
    # Config off: the first fly is a click. Auto Open only adds while a fly is active.
    strategy.arm_cookie = spec is not None
    _arm_clocks(strategy, spec, auto_open=bool(engine["auto_open"]))
    if spec is not None and spec.auto.get("add_until"):
        strategy.add_until = time.fromisoformat(str(spec.auto["add_until"]))
    width_choice = spec.wings if spec is not None else str(load_wings(conn))
    if spec is not None and not rebuild and spec.desk == "auto":
        width_choice = _starting_wings(prior_wings, width_choice)
    regime_latch = None if rebuild else _regime_latch(conn)
    if rebuild:
        conn.execute("UPDATE desk_state SET regime_latch = NULL WHERE player_id = ?", (_pid(),))
        forget_policy(_pid())
    walked = 0
    logged = None
    after = int(applied.timestamp()) if applied is not None else None
    pct_low, em_low = _tape_floor(marks.tape or [], after)
    until_s = int(until.timestamp())
    if marks.tape:
        until_s = min(until_s, int(marks.tape[-1]["time"]))
    until = datetime.fromtimestamp(until_s, tz=_ET)
    points = _spx_slice(day, after, until_s)
    if points is None:
        points = tape_seconds(marks.tape or [], after, until_s)
    for point in points:
        ts = datetime.fromtimestamp(int(point["time"]), tz=_ET)
        wall = book_clock(ts)
        # Fill slippage evaluates EM picks from desk_state, just like a clock
        # pick. Publish this print before any fills, and clear missing readings
        # so an earlier low EM cannot keep a pick active through a data gap.
        conn.execute(
            "UPDATE desk_state SET em_pct = ?, em_now = ? WHERE player_id = ?",
            (_point_num(point.get("pct")), _point_num(point.get("em")), _pid()),
        )
        if spec is not None:
            pct = _point_num(point.get("pct"))
            em_value = _point_num(point.get("em"))
            pct_low = _floor(pct_low, pct)
            em_low = _floor(em_low, em_value)
            state = apply_config_state(
                spec,
                book,
                spot=float(point["value"]),
                em_pct=pct,
                wall=wall,
                wing_width=_point_num(point.get("wings")),
                wings=width_choice,
                em_now=em_value,
                latest_wings=width_choice if spec.desk == "auto" else None,
                em_pct_low=pct_low,
                em_low=em_low,
                regime_latch=regime_latch,
            )
            regime_latch = state.regime_latch or None
            width_choice = state.latest_wings or state.wings
            shown = state.wings if state.wings != "auto" else str(wings_for_clock(wall))
            note_policy(conn, ts, state.regime, int(state.stop), shown)
            if engine["auto_open"]:
                strategy.f1_at = state.f1_at
        clock = _clock_text(ts)
        if leftover_long_rows(conn) and leftovers_sell_allowed(book_clock(ts), leftover_rules):
            sold = sell_ready_leftovers(conn, settings, chain_asof(settings, day, ts), leftover_rules, ts)
            if sold:
                log.info("step %s sold %s leftover longs", clock, sold)
        strategy.on_quote(Quote(symbol="SPX", last=float(point["value"]), ts=ts))
        wings = None if width_choice == "auto" else int(width_choice)
        _fill_due_replay_closes(conn, settings, day, strategy, ts, wings or wings_for_clock(wall))
        intents = strategy.decide(float(point["value"]), ts, wings=wings)
        signature = (
            point.get("value"),
            point.get("pct"),
            point.get("wings"),
            book.regime,
            book.stop_pad,
            width_choice,
            bool(intents),
        )
        if signature != logged:
            logged = signature
            log.info(
                "step %s read spx=%.2f em%%=%s em=%s mkt_wings=%s",
                clock,
                float(point["value"]),
                point.get("pct"),
                point.get("em"),
                point.get("wings"),
            )
            log.info("step %s config %s", clock, _config_trace(spec, book, width_choice, point))
            log.info("step %s flies %s", clock, _fly_trace(book))
            log.info("step %s watch %s", clock, _watch_trace(strategy.book.log_ladder(float(point["value"]), ts)))
            if not intents:
                log.info("step %s decision hold", clock)
        for intent in intents:
            strike = intent.payload.get("strike")
            blocked = (intent.kind is IntentKind.ADD and not engine["auto_open"]) or (
                intent.kind is IntentKind.CLOSE and not engine["auto_drop"]
            )
            if blocked:
                log.info("step %s decision %s %s blocked %s", clock, intent.kind.value, strike, intent.reason)
                continue
            log.info("step %s decision %s %s %s", clock, intent.kind.value, strike, intent.reason)
            _dispatch_replay_intent(
                conn, settings, day, strategy, intent, ts, wings or wings_for_clock(wall), delay_ms
            )
            if getattr(strategy, "opens_paused", False):
                engine["auto_open"] = False
        walked += 1
    log.info("step %s walked %s prints", _clock_text(until), walked)
    save_book(conn, book)
    _save_regime_latch(conn, regime_latch)
    if engine["use_config"]:
        conn.execute(
            "UPDATE desk_state SET regime = ?, stop_pad = ? WHERE player_id = ?",
            (saved.regime, saved.stop_pad, _pid()),
        )
    conn.execute("UPDATE desk_state SET book_day = ? WHERE player_id = ?", (day.isoformat(), _pid()))
    _set_applied(conn, until)
    if spec is not None and walked:
        _adopt_state(
            conn,
            engine,
            ConfigState(book.regime, width_choice, int(book.stop_pad), strategy.f1_at, width_choice),
            book_clock(until),
        )
    else:
        resolve_engine(conn, book_clock(until))
    return until


def _dispatch_replay_intent(conn, settings, day, strategy, intent, ts, wings, delay_ms):
    """Queue closes without reading any future quote; opens and zero delay fill now."""
    if intent.kind is not IntentKind.CLOSE or delay_ms <= 0:
        return _fill_intent(conn, settings, day, strategy, intent, ts, wings)
    if intent.reason == "flatten_at":
        pause_auto_open(conn)
        strategy.opens_paused = True
        for fly in strategy.book.active():
            one = Intent(IntentKind.CLOSE, "flatten", {"strike": fly.strike, "wings": fly.wings})
            _dispatch_replay_intent(conn, settings, day, strategy, one, ts, wings, delay_ms)
        return False
    fly = _active_fly(strategy.book, float(intent.payload["strike"]))
    if fly is None:
        return False
    conn.execute(
        "INSERT OR IGNORE INTO replay_pending_close "
        "(player_id, strike, opened_at, due_at, reason, wings) VALUES (?, ?, ?, ?, ?, ?)",
        (_pid(), fly.strike, fly.opened_at.isoformat(),
         (ts + timedelta(milliseconds=delay_ms)).isoformat(), intent.reason, fly.wings),
    )
    return False


def _fill_due_replay_closes(conn, settings, day, strategy, ts, wings):
    rows = conn.execute(
        "SELECT * FROM replay_pending_close WHERE player_id = ? ORDER BY due_at, strike", (_pid(),)
    ).fetchall()
    for row in rows:
        key = (_pid(), row["strike"], row["opened_at"])
        fly = _active_fly(strategy.book, float(row["strike"]))
        stale = fly is None or fly.opened_at.isoformat() != row["opened_at"]
        if stale or book_clock(ts) >= TRADE_STOP:
            conn.execute(
                "DELETE FROM replay_pending_close WHERE player_id = ? AND strike = ? AND opened_at = ?", key
            )
            continue
        if ts < datetime.fromisoformat(row["due_at"]):
            continue
        intent = Intent(IntentKind.CLOSE, row["reason"], {"strike": row["strike"], "wings": row["wings"]})
        if _fill_intent(conn, settings, day, strategy, intent, ts, wings):
            conn.execute(
                "DELETE FROM replay_pending_close WHERE player_id = ? AND strike = ? AND opened_at = ?", key
            )


def _applied_at(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT applied_at FROM desk_state WHERE player_id = ?", (_pid(),)).fetchone()
    if row is None or not row["applied_at"]:
        return None
    return datetime.fromisoformat(row["applied_at"])


def _set_applied(conn: sqlite3.Connection, when: datetime) -> None:
    _ensure_state(conn)
    conn.execute("UPDATE desk_state SET applied_at = ? WHERE player_id = ?", (when.isoformat(), _pid()))


def watch_frame(conn: sqlite3.Connection, settings: Settings, marks: DeskMarks) -> dict:
    model = sheet(conn, settings, marks, brief=True)
    ladder = []
    for row in model.get("ladder") or []:
        ladder.append(
            {
                "watch": row.get("watch"),
                "kind": row.get("kind"),
                "strike": row.get("strike"),
                "var": row.get("var"),
                "enabled": bool(row.get("enabled")),
                "rule": bool(row.get("rule")),
            }
        )
    flies = []
    for row in model.get("flies") or []:
        if row.get("status") == "empty":
            continue
        flies.append(
            {
                "slot": row.get("slot"),
                "strike": row.get("strike"),
                "wings": row.get("put_wing"),
                "call_wings": row.get("call_wing"),
                "status": row.get("status"),
                "time_open": row.get("time_open") or "",
                "time_close": row.get("time_close") or "",
                "origin": row.get("origin") or "",
                "sto": row.get("sto"),
                "btc": row.get("btc_actual"),
                "pnl": row.get("pnl_mid"),
                "dead_longs": row.get("dead_longs") or [],
                "sales": row.get("sales") or [],
            }
        )
    return {
        "ladder": ladder,
        "flies": flies,
        "manual": not model["auto_open"] and not model["auto_drop"],
        "lines": line_payload(model),
        "engine": {
            "regime": model["regime"],
            "stop": model["stop_pad"],
            "wings": model.get("wing_choice"),
            "line": model["engine_line"],
            "use_config": model["use_config"],
            "auto_open": model["auto_open"],
            "auto_drop": model["auto_drop"],
            "open_paused": model["open_paused"],
        },
        "leftover_longs": model.get("leftover_longs") or [],
        "dead_longs": model.get("dead_longs") or [],
        "open_conflict": model.get("open_conflict"),
        "pnl": model.get("pnl_mid_total"),
        "parked_cash_points": model.get("parked_cash_points"),
        "parked_value_points": model.get("parked_value_points"),
        "last": model.get("last_price"),
        "em_open": model.get("em_open"),
        "em_now": model.get("em_now"),
        "em_pct": model.get("em_pct"),
        "put_40_width": model.get("put_40_width"),
        "max_profit": model.get("max_profit"),
        "max_profit_at": model.get("max_profit_at"),
        "max_profit_at_hi": model.get("max_profit_at_hi"),
        "max_profit_n": model.get("max_profit_n"),
    }


def _clicks_allowed(settings: Settings, run_mode: str) -> bool:
    if run_mode == "auto":
        return False
    return settings.desk_mode in BOOK_FILLS or settings.desk_mode in BACKTEST_MODES


def _saved_spec(conn: sqlite3.Connection) -> DeskSpec | None:
    text = load_spec_yaml(conn).strip()
    if not text:
        return None
    try:
        return parse_spec(text)
    except ValueError:
        return None


def _point_num(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def session_step(conn: sqlite3.Connection, settings: Settings, marks: DeskMarks) -> None:
    """One wall-clock decision, stored in this database. The browser is not required."""
    if settings.is_backtest or settings.desk_mode not in BOOK_FILLS:
        return
    if marks.last is None:
        return
    with _BOOK_LOCK:
        clock = _clock(marks.ts)
        _latch_em(conn, marks, clock)
        wall = book_clock(clock)
        pct_low, em_low = session_floors(conn, settings, marks)
        engine = resolve_engine(
            conn,
            wall,
            em_pct=marks.em_pct,
            em_now=marks.em_now,
            em_pct_low=pct_low,
            em_low=em_low,
        )
        spot = float(marks.last)
        book = load_book(conn)
        if engine["use_config"]:
            book.regime = str(engine["regime"])
            book.stop_pad = int(engine["stop"])
        strategy = DaleOg(book)
        strategy.on_quote(Quote(symbol="SPX", last=spot, bid=marks.bid, ask=marks.ask, ts=clock))
        mode = book.mode_at(clock).name
        prev = _PREV_PRINT.get(_pid())
        if not engine["auto_open"] and not engine["auto_drop"]:
            _PREV_PRINT[_pid()] = (spot, mode)
            return
        if prev is not None:
            strategy._prev_spot = prev[0]
            strategy._prev_mode = prev[1]
        spec = _saved_spec(conn) if engine["use_config"] else None
        strategy.arm_cookie = spec is not None
        width_choice = spec.wings if spec is not None else str(load_wings(conn))
        if spec is not None and spec.desk != "auto":
            width_choice = spec.wings
        _arm_clocks(strategy, spec, auto_open=bool(engine["auto_open"]), em_pct=marks.em_pct, wall=wall, book=book)
        if spec is not None:
            state = apply_config_state(
                spec,
                book,
                spot=spot,
                em_pct=marks.em_pct,
                wall=wall,
                wing_width=marks.put_40_width,
                wings=width_choice,
                em_now=marks.em_now,
                latest_wings=str(engine["wings"]) if spec.desk == "auto" else None,
                em_pct_low=pct_low,
                em_low=em_low,
                regime_latch=_regime_latch(conn),
            )
            _save_regime_latch(conn, state.regime_latch)
            width_choice = state.latest_wings or state.wings
            if engine["auto_open"]:
                strategy.f1_at = state.f1_at
            raw_until = spec.auto.get("add_until")
            if raw_until:
                strategy.add_until = time.fromisoformat(str(raw_until))
            shown = state.wings if state.wings != "auto" else str(wings_for_clock(wall))
            note_policy(conn, clock, state.regime, int(state.stop), shown)
        else:
            note_policy(conn, clock, str(engine["regime"]), int(engine["stop"]), str(engine["wings"]))
        wings = None if width_choice == "auto" else int(width_choice)
        intents = strategy.decide(spot, clock, wings=wings)
        filled = False
        for intent in intents:
            blocked = (intent.kind is IntentKind.ADD and not engine["auto_open"]) or (
                intent.kind is IntentKind.CLOSE and not engine["auto_drop"]
            )
            if blocked:
                log.info(
                    "step %s decision %s %s blocked",
                    _clock_text(clock),
                    intent.kind.value,
                    intent.payload.get("strike"),
                )
                continue
            log.info(
                "step %s decision %s %s %s",
                _clock_text(clock),
                intent.kind.value,
                intent.payload.get("strike"),
                intent.reason,
            )
            filled = (
                _fill_intent(
                    conn,
                    settings,
                    clock.date(),
                    strategy,
                    intent,
                    clock,
                    wings or wings_for_clock(wall),
                    marks.chain,
                )
                or filled
            )
        if filled:
            save_book(conn, book)
        _PREV_PRINT[_pid()] = (spot, book.mode_at(clock).name)


def _arm_clocks(
    strategy: DaleOg,
    spec: DeskSpec | None,
    *,
    auto_open: bool,
    em_pct: float | None = None,
    wall: time | None = None,
    book: FlyBook | None = None,
) -> None:
    """Bind flatten_at and F1 for manual and auto. The overlay stays on auto."""
    from iron_fly.strategy.config_state import _f1_at

    if spec is None:
        strategy.flatten_at = None
        return
    raw = spec.auto.get("flatten_at")
    strategy.flatten_at = time.fromisoformat(str(raw)) if raw else None
    if auto_open and book is not None and wall is not None:
        strategy.f1_at = _f1_at(spec, book, em_pct, wall)
    elif auto_open and spec.f1_at is not None:
        strategy.f1_at = spec.f1_at


def _buy_shorts_sell_live_wings(
    conn: sqlite3.Connection,
    settings: Settings,
    chain,
    strike: float,
    wings: int,
    clock: str,
    when: datetime | None = None,
    call_wings: int | None = None,
) -> tuple[float | None, int, str | None, list[dict]]:
    """BTC the shorts and STC each wing that has a bid. Park the rest.

    The sale is part of this stop, on the same print. Returns the debit before
    slippage, the contracts traded, an error code, and the wings that sold.
    """
    sold, parked = live_wing_bids(chain, strike, wings, call_wings)
    _, close_mode = _fill_modes(conn)
    mark = short_mark(chain, strike, mode=close_mode)
    if mark is None:
        return None, 0, "nomid", []
    rejected = _send_shorts(settings, conn, chain, strike)
    if rejected:
        return None, 0, rejected, []
    contracts = _SHORT_LEGS
    filled: list[dict] = []
    for lot in sold:
        rejected = _send_one_long(settings, conn, str(lot["occ"]))
        qty = int(lot.get("quantity") or 1)
        if rejected:
            parked.append(lot)
            log.info(
                "step %s long %s %s rejected %s",
                clock,
                lot["right"],
                int(float(lot["strike"])),
                rejected,
            )
            continue
        mark = round(float(mark) - float(lot["bid"]) * qty, 2)
        contracts += qty
        filled.append(lot)
        log.info(
            "step %s sold long %s %s %.2f",
            clock,
            lot["right"],
            int(float(lot["strike"])),
            float(lot["bid"]),
        )
    if parked:
        park_lots(conn, parked)
        note = leftover_long_note(parked)
        if note:
            log.info("step %s %s", clock, note)
    if when is not None:
        note_event(conn, when, f"Closed straddle {int(strike)}")
        for lot in filled:
            note_event(
                conn,
                when,
                f"Closed long {lot['right']} {int(float(lot['strike']))} at {float(lot['bid']):.2f}",
            )
        if parked:
            strikes = " ".join(str(int(float(lot["strike"]))) for lot in parked)
            note_event(conn, when, f"Parked longs {strikes}")
    return float(mark), contracts, None, filled


def _release_blocking_longs(conn, book, sales: list[dict], clock: str, when: datetime | None = None) -> None:
    """STC parked longs that are the new short, and book each sale on its fly."""
    if not sales:
        return
    release_longs(conn, sales)
    fee = _fee_points(conn, 1)
    for lot in sales:
        _credit_wing_sale(book, lot, fee)
        if when is not None:
            note_event(
                conn,
                when,
                f"Closed long {lot['right']} {int(float(lot['strike']))} at {float(lot['bid']):.2f}",
            )
        log.info(
            "step %s sold long %s %s %.2f",
            clock,
            lot["right"],
            int(float(lot["strike"])),
            float(lot["bid"]),
        )


def _credit_wing_sale(book, lot: dict, fee_each: float) -> None:
    """Take the wing sale off the closed fly's debit, net of one fee per contract."""
    right = str(lot["right"])
    strike = float(lot["strike"])
    qty = int(lot.get("quantity") or 1)
    points = float(lot["bid"]) * qty - fee_each * qty
    owners = []
    for fly in book.flies:
        if fly.status != "closed" or fly.close_debit is None:
            continue
        put_w, call_w = fly_spans(fly)
        wing = fly.strike + call_w if right == "call" else fly.strike - put_w
        if int(round(wing)) == int(round(strike)):
            owners.append(fly)
    if not owners:
        log.info("step wing sale %s %s %.2f has no closed fly", right, int(strike), points)
        return
    fly = max(owners, key=lambda item: item.closed_at or datetime.min.replace(tzinfo=_ET))
    fly.close_debit = round(float(fly.close_debit) - points, 2)
    _remember_sales(fly, [lot])


def _fill_intent(
    conn: sqlite3.Connection,
    settings: Settings,
    day: date,
    strategy: DaleOg,
    intent,
    ts: datetime,
    wings: int,
    chain=None,
) -> bool:
    if intent.kind is IntentKind.CLOSE and intent.reason == "flatten_at":
        pause_auto_open(conn)
        strategy.opens_paused = True
        filled = False
        for fly in list(strategy.book.active()):
            one = Intent(IntentKind.CLOSE, "flatten", {"strike": fly.strike, "wings": fly.wings})
            filled = _fill_intent(conn, settings, day, strategy, one, ts, wings, chain) or filled
        return filled
    strike = float(intent.payload["strike"])
    width = int(intent.payload.get("wings") or wings)
    clock = _clock_text(ts)
    if chain is None and settings.desk_mode not in BOOK_FILLS:
        chain = chain_asof(settings, day, ts)
    if intent.kind is IntentKind.ADD:
        requested = width
        chosen = widen_blocked_open(strategy.book.active(), strike, requested)
        if chosen is None:
            book_hold = open_book_hold(strategy.book.active(), strike, requested) or ""
            record_open_blocked(conn, ts, strike, requested, book_hold, strategy.book.active())
            log.info("step %s fill open %s skipped %s", clock, int(strike), book_hold)
            return False
        width, call_w = chosen
        conflict, wing_sales = priced_short_conflicts(conn, chain, strike)
        if conflict:
            record_open_blocked(conn, ts, strike, requested, conflict, strategy.book.active())
            log.info("step %s fill open %s skipped %s", clock, int(strike), conflict)
            return False
        reused = reusable_wing_lots(conn, chain, strike, width, call_w)
        # Backtest books the chain mid. Refuse (and retry next tick) until every
        # leg is quoted — never STO at the 0.01 penny placeholder.
        if settings.desk_mode in BACKTEST_MODES:
            mark = _subtract_open_slippage(
                conn,
                ts,
                _sim_open_credit(conn, chain, strike, width, reused=reused is not None, call_wings=call_w),
                _SHORT_LEGS if reused is not None else _FLY_LEGS,
            )
            if mark is None:
                log.warning(
                    "step %s fill open %s deferred no quotes %s (retry)",
                    clock,
                    int(strike),
                    leg_book(chain, strike, width, call_w),
                )
                return False
            if reused is not None:
                consume_wing_lots(conn, reused)
            _release_blocking_longs(conn, strategy.book, wing_sales, clock, ts)
            intent.payload["credit"] = mark
            intent.payload["wings"] = width
            intent.payload["call_wings"] = call_w
            strategy.apply_intent(intent, ts)
            _attach_call_wings(_active_fly(strategy.book, strike), call_w)
            clear_open_conflict(conn)
            kind = "Opened straddle" if reused is not None else "Opened fly"
            note_event(conn, ts, f"{kind} {int(strike)} ({_fly_legs(strike, width, call_w)}) at {float(mark):.2f}")
            log.info(
                "step %s fill open %s credit=%.2f wings=%s %s%s",
                clock,
                int(strike),
                mark,
                width,
                leg_book(chain, strike, width, call_w),
                " reused longs" if reused is not None else "",
            )
            return True
        if reused is not None:
            rejected, fill = _send_open_shorts(settings, conn, chain, strike)
            if rejected:
                log.info("step %s fill open %s rejected %s", clock, int(strike), rejected)
                return False
            mark = _straddle_credit(fill, chain, strike)
            if mark is None:
                log.warning(
                    "step %s fill open %s deferred no straddle credit for reused longs",
                    clock,
                    int(strike),
                )
                return False
            consume_wing_lots(conn, reused)
            _release_blocking_longs(conn, strategy.book, wing_sales, clock, ts)
            intent.payload["credit"] = mark
            intent.payload["wings"] = width
            intent.payload["call_wings"] = call_w
            strategy.apply_intent(intent, ts)
            _attach_call_wings(_active_fly(strategy.book, strike), call_w)
            clear_open_conflict(conn)
            note_event(conn, ts, f"Opened straddle {int(strike)} ({_fly_legs(strike, width, call_w)}) at {float(mark):.2f}")
            log.info(
                "step %s fill open %s credit=%.2f wings=%s reused longs",
                clock,
                int(strike),
                mark,
                width,
            )
            return True
        rejected, fill = _send_live(settings, conn, chain, strike, width, closing=False)
        if rejected:
            log.info("step %s fill open %s rejected %s", clock, int(strike), rejected)
            return False
        open_mode, _ = _fill_modes(conn)
        mark = _book_package_price(fill, None, chain, strike, width, side="open", mode=open_mode, call_wings=call_w)
        if mark is None or float(mark) <= MIN_OPEN_CREDIT:
            if fill is not None and abs(float(fill)) > MIN_OPEN_CREDIT:
                log.error(
                    "step %s fill open %s booked without mid after live fill=%s",
                    clock,
                    int(strike),
                    fill,
                )
                mark = abs(float(fill))
            else:
                log.warning(
                    "step %s fill open %s deferred no fill after live order fill=%s",
                    clock,
                    int(strike),
                    fill,
                )
                return False
        _release_blocking_longs(conn, strategy.book, wing_sales, clock, ts)
        intent.payload["credit"] = mark
        intent.payload["wings"] = width
        intent.payload["call_wings"] = call_w
        strategy.apply_intent(intent, ts)
        _attach_call_wings(_active_fly(strategy.book, strike), call_w)
        clear_open_conflict(conn)
        note_event(conn, ts, f"Opened fly {int(strike)} ({_fly_legs(strike, width, call_w)}) at {float(mark):.2f}")
        log.info(
            "step %s fill open %s credit=%.2f wings=%s %s",
            clock,
            int(strike),
            mark,
            width,
            leg_book(chain, strike, width, call_w),
        )
        return True
    if intent.kind is IntentKind.CLOSE:
        fly = next((item for item in strategy.book.active() if item.strike == strike), None)
        if fly is None:
            return False
        width, call_w = fly_spans(fly)
        if dead_wings(chain, strike, width, call_w):
            mark, contracts, rejected, sold = _buy_shorts_sell_live_wings(
                conn, settings, chain, strike, width, clock, ts, call_wings=call_w
            )
            _remember_sales(fly, sold)
            if rejected == "nomid" or mark is None:
                log.info("step %s fill drop %s skipped no chain mid wings=%s", clock, int(strike), width)
                return False
            if rejected:
                log.info("step %s fill drop %s rejected %s", clock, int(strike), rejected)
                return False
        else:
            contracts = _FLY_LEGS
            rejected, fill = _send_live(settings, conn, chain, strike, width, closing=True)
            if rejected:
                log.info("step %s fill drop %s rejected %s", clock, int(strike), rejected)
                return False
            _, close_mode = _fill_modes(conn)
            mark = _book_package_price(
                fill, None, chain, strike, width, side="close", mode=close_mode, call_wings=call_w
            )
            if mark is None or float(mark) <= MIN_OPEN_CREDIT:
                if fill is not None and abs(float(fill)) > MIN_OPEN_CREDIT:
                    log.error(
                        "step %s fill drop %s booked without mid after live fill=%s",
                        clock,
                        int(strike),
                        fill,
                    )
                    mark = abs(float(fill))
                else:
                    log.info(
                        "step %s fill drop %s skipped no chain mid wings=%s",
                        clock,
                        int(strike),
                        width,
                    )
                    return False
        mark = _add_close_slippage(conn, ts, mark, contracts)
        if not dead_wings(chain, strike, width, call_w):
            note_event(conn, ts, f"Closed fly {int(strike)} at {float(mark):.2f}")
        intent.payload["debit"] = mark
        intent.payload["wings"] = width
        strategy.apply_intent(intent, ts)
        log.info(
            "step %s fill drop %s debit=%.2f wings=%s %s",
            clock,
            int(strike),
            mark,
            width,
            leg_book(chain, strike, width, call_w),
        )
        return True
    return False


def _clock(when: datetime | None) -> datetime:
    clock = when or datetime.now(_ET)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=_ET)
    return clock.astimezone(_ET)


def preview_fly(
    conn: sqlite3.Connection,
    settings: Settings,
    marks: DeskMarks,
    strike: float,
    wings: int,
) -> str:
    """Preview a market open. Other modes only show the combo mid."""
    width = wings if wings in FLY_WIDTHS else load_wings(conn)
    mark = combo_mark(marks.chain, strike, width)
    if mark is None:
        return "No combo mid for that fly."
    return f"Sell {int(strike - width)}/{int(strike)}/{int(strike + width)} for {mark:.2f} credit."


def _leftover_auto(conn: sqlite3.Connection) -> dict[str, object] | None:
    spec = _saved_spec(conn)
    return None if spec is None else spec.auto


def _straddle_credit(fill: float | None, chain, strike: float) -> float | None:
    """Raw short-straddle credit for a reused-wing open. None when unquoted."""
    if fill is not None:
        value = abs(float(fill))
        if value > MIN_OPEN_CREDIT and value <= 1000:
            return value
    mark = short_mark(chain, strike)
    if mark is not None and mark > MIN_OPEN_CREDIT and mark <= 1000:
        return float(mark)
    return None


def _sim_open_credit(
    conn: sqlite3.Connection,
    chain,
    strike: float,
    width: int,
    *,
    reused: bool,
    call_wings: int | None = None,
) -> float | None:
    """Chain credit for a backtest open. None until every needed leg is quoted."""
    if reused:
        return _straddle_credit(None, chain, strike)
    open_mode, _ = _fill_modes(conn)
    mark = _book_package_price(
        None, None, chain, strike, width, side="open", mode=open_mode, call_wings=call_wings
    )
    if mark is None or float(mark) <= MIN_OPEN_CREDIT:
        return None
    return float(mark)


def _send_open_shorts(
    settings: Settings,
    conn: sqlite3.Connection,
    chain,
    strike: float,
    *,
    expiration: date | None = None,
) -> tuple[str | None, float | None]:
    """Live order sends are not available in the public desk."""
    return None, None


def _send_live(
    settings: Settings,
    conn: sqlite3.Connection,
    chain,
    strike: float,
    wings: int,
    *,
    closing: bool,
) -> tuple[str | None, float | None]:
    """Live order sends are not available in the public desk."""
    return None, None


def _buy_shorts_then_wings(conn, settings: Settings, fly, chain, clock: datetime) -> tuple[float, str | None]:
    """BTC the shorts, then STC the wings. The shorts do not wait on a chain."""
    when = clock.date()
    if chain is not None and chain.expiration is not None:
        when = chain.expiration
    rejected = _send_shorts(settings, conn, chain, fly.strike, expiration=when)
    if rejected:
        return fly.credit, rejected
    put_w, call_w = fly_spans(fly)
    _sell_each_long(conn, settings, chain, fly.strike, put_w, when, clock, call_wings=call_w)
    _, close_mode = _fill_modes(conn)
    debit = combo_fill(chain, fly.strike, put_w, side="close", mode=close_mode, call_wings=call_w)
    if debit is None:
        debit = float(fly.credit)
    return debit, None


def _built_wings(expiration: date, strike: float, wings: int, call_wings: int | None = None) -> list[dict]:
    call = strike + (wings if call_wings is None else call_wings)
    return [
        {
            "occ": spxw_symbol(expiration, "put", strike - wings),
            "right": "put",
            "strike": float(strike - wings),
            "quantity": 1,
        },
        {
            "occ": spxw_symbol(expiration, "call", call),
            "right": "call",
            "strike": float(call),
            "quantity": 1,
        },
    ]


def _send_shorts(
    settings: Settings,
    conn: sqlite3.Connection,
    chain,
    strike: float,
    *,
    expiration: date | None = None,
) -> str | None:
    """Live order sends are not available in the public desk."""
    return None


def _sell_each_long(
    conn,
    settings: Settings,
    chain,
    strike: float,
    wings: int,
    expiration: date,
    clock: datetime,
    call_wings: int | None = None,
) -> None:
    """STC each long by itself. A wing with no bid stays parked."""
    parked = []
    for lot in _built_wings(expiration, strike, wings, call_wings):
        symbol = _quoted_symbol(chain, str(lot["right"]), float(lot["strike"])) or str(lot["occ"])
        lot["occ"] = symbol
        reason = _no_bid(chain, str(lot["right"]), float(lot["strike"]))
        if reason:
            parked.append(lot)
            log.info("step %s park %s %s %s", _clock_text(clock), lot["right"], int(lot["strike"]), reason)
            continue
        rejected = _send_one_long(settings, conn, symbol)
        if rejected:
            parked.append(lot)
            log.info(
                "step %s long %s %s rejected %s",
                _clock_text(clock),
                lot["right"],
                int(lot["strike"]),
                rejected,
            )
    if parked:
        park_lots(conn, parked)
        note = leftover_long_note(parked)
        if note:
            log.info("step %s %s", _clock_text(clock), note)


def _quoted_symbol(chain, right: str, strike: float) -> str:
    if chain is None:
        return ""
    for row in chain.strikes:
        if row.strike != strike:
            continue
        symbol = row.call_symbol if right == "call" else row.put_symbol
        return symbol or ""
    return ""


def _no_bid(chain, right: str, strike: float) -> str | None:
    """A known quote with no bid. An absent chain still tries the order."""
    if chain is None:
        return None
    for row in chain.strikes:
        if row.strike != strike:
            continue
        bid, ask = (row.call_bid, row.call_ask) if right == "call" else (row.put_bid, row.put_ask)
        return dead_reason(bid, ask)
    return "no quote"


def _send_one_long(settings: Settings, conn: sqlite3.Connection, occ: str) -> str | None:
    """Live order sends are not available in the public desk."""
    return None


def _send_roll(
    settings: Settings,
    conn: sqlite3.Connection,
    close_occ: str,
    open_occ: str,
    quantity: int,
) -> str | None:
    """Live order sends are not available in the public desk."""
    return None


def _send_leftover(settings: Settings, conn: sqlite3.Connection, occ: str, quantity: int) -> str | None:
    """Live order sends are not available in the public desk."""
    return None



def _latch_em(conn: sqlite3.Connection, marks: DeskMarks, clock: datetime) -> None:
    if marks.last is not None and marks.em_now is None:
        marks.em_now = straddle_mid(marks.chain, round_to_5(marks.last))
    if marks.last is not None and marks.put_40_width is None:
        marks.put_40_width = put40_width(marks.chain, round_to_5(marks.last))
    day = clock.date().isoformat()
    stored, samples = _open_samples(conn, day)
    if marks.em_open is None:
        marks.em_open = stored
    if marks.em_open is None and marks.last is not None:
        wall = clock.time()
        stamp = int(clock.timestamp())
        sample = _opening_sample(marks.chain, marks.last)
        seen = {item[0] for item in samples}
        if sample is not None and _OPEN <= wall <= _OPEN_AVG_END and stamp not in seen:
            samples.append((stamp, sample))
            _save_open(conn, day, None, samples)
        values = [value for _, value in samples]
        if values and wall > _OPEN_AVG_END:
            marks.em_open = round(sum(values) / len(values), 1)
            _save_open(conn, day, marks.em_open, samples)
        elif not values and sample is not None and _OPEN_AVG_END < wall < _OPEN_LATCH_END:
            marks.em_open = round(sample, 1)
            _save_open(conn, day, marks.em_open, samples)
    if marks.em_pct is None and marks.em_open and marks.em_now is not None and marks.em_open > 0:
        marks.em_pct = round(100 * marks.em_now / marks.em_open, 1)


def _opening_sample(chain, spot: float) -> float | None:
    """Two-sided ATM straddle. A zero bid or ask is skipped, as on the old desk."""
    if chain is None:
        return None
    strike = round_to_5(spot)
    row = next((item for item in chain.strikes if item.strike == strike), None)
    if row is None:
        return None
    if not _two_sided(row.call_bid, row.call_ask) or not _two_sided(row.put_bid, row.put_ask):
        return None
    call = (float(row.call_bid) + float(row.call_ask)) / 2
    put = (float(row.put_bid) + float(row.put_ask)) / 2
    return round(call + put, 4)


def _two_sided(bid: float | None, ask: float | None) -> bool:
    return bid is not None and ask is not None and bid > 0 and ask > 0


def _open_samples(conn: sqlite3.Connection, day: str) -> tuple[float | None, list[tuple[int, float]]]:
    _ensure_state(conn)
    row = conn.execute(
        "SELECT em_open, em_open_day, em_open_samples FROM desk_state WHERE player_id = ?",
        (_pid(),),
    ).fetchone()
    if row is None or row["em_open_day"] != day:
        return None, []
    opening = None if row["em_open"] is None else float(row["em_open"])
    try:
        raw = json.loads(row["em_open_samples"] or "[]")
    except json.JSONDecodeError:
        raw = []
    samples: list[tuple[int, float]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, list) and len(item) == 2:
                samples.append((int(item[0]), float(item[1])))
    return opening, samples


def _save_open(conn: sqlite3.Connection, day: str, opening: float | None, samples: list[tuple[int, float]]) -> None:
    _ensure_state(conn)
    payload = json.dumps([[stamp, value] for stamp, value in samples])
    conn.execute(
        "UPDATE desk_state SET em_open = ?, em_open_day = ?, em_open_samples = ? WHERE player_id = ?",
        (opening, day, payload, _pid()),
    )


def _stamp_flies(data: dict, book: FlyBook, marks: DeskMarks, clock: datetime) -> None:
    rows = data.get("flies") or []
    data["flies"] = rows
    paired = [row for row in rows if row.get("status") != "empty"]
    flies = list(book.flies)
    while len(paired) < len(flies):
        fly = flies[len(paired)]
        put_w, call_w = fly_spans(fly)
        row = {
            "slot": len(paired) + 1,
            "strike": fly.strike,
            "status": "closed" if fly.status == "closed" else "active",
            "put_wing": put_w,
            "call_wing": call_w,
            "sto": fly.credit,
            "btc_actual": fly.close_debit,
            "net": fly.net,
            "origin": fly.origin,
        }
        rows.append(row)
        paired.append(row)
    if len(paired) > len(flies):
        paired = paired[: len(flies)]
    pnl = 0.0
    seen = False
    cash = book_clock(clock) >= _CASH_AT
    close = _cash_close(marks) if cash else None
    for row, fly in zip(paired, flies):
        row["sales"] = _sale_rows(getattr(fly, "sales", []))
        row["time_open"] = fly.opened_at.astimezone(_ET).strftime("%H:%M:%S")
        if fly.closed_at is not None:
            row["time_close"] = fly.closed_at.astimezone(_ET).strftime("%H:%M:%S")
        settled = cash and close is not None and _settle_fly(fly)
        if settled:
            put_w, call_w = fly_spans(fly)
            row["pnl_mid"] = round(
                fly_expiration_pnl(close, fly.strike, fly.credit, put_w, call_w),
                2,
            )
        elif fly.status == "closed" and fly.net is not None:
            row["pnl_mid"] = fly.net
        else:
            put_w, call_w = fly_spans(fly)
            mark = combo_mark(marks.chain, fly.strike, put_w, call_w)
            row["pnl_mid"] = None if mark is None else round(fly.credit - mark, 2)
        if fly.status == "active":
            put_w, call_w = fly_spans(fly)
            row["dead_longs"] = dead_wings(marks.chain, fly.strike, put_w, call_w)
        else:
            row["dead_longs"] = []
        if row["pnl_mid"] is not None:
            pnl += float(row["pnl_mid"])
            seen = True
    data["pnl_mid_total"] = pnl if seen else None


def _settle_fly(fly) -> bool:
    """Open flies, and any fly closed after the cash print, settle to the 16:00 SPX."""
    if fly.status != "closed" or fly.closed_at is None:
        return True
    return book_clock(fly.closed_at) >= _CASH_AT


def remember_cash_close(when: datetime, value: float) -> None:
    """Provisional SPX close before the 16:05 HTTP fetch."""
    clock = _clock(when)
    if book_clock(clock) >= _CASH_FETCH_AT:
        return
    _CASH_CLOSE[clock.date().isoformat()] = float(value)


def ensure_cash_close(conn: sqlite3.Connection, settings: Settings, marks: DeskMarks, clock: datetime) -> None:
    """Settle cash close: live desks GET /v1/spx `last` once at/after 16:05 ET."""
    wall = book_clock(clock)
    if wall < _CASH_AT:
        return
    day = _clock(clock).astimezone(_ET).date()
    key = day.isoformat()
    found: float | None = None

    if settings.desk_mode in BACKTEST_MODES:
        found = _tape_cash(marks, day) or _CASH_CLOSE.get(key) or _load_cash_close(conn, key)
    else:
        if marks.last is not None:
            remember_cash_close(clock, float(marks.last))
        found = _CASH_CLOSE.get(key) or _tape_cash(marks, day) or _load_cash_close(conn, key)

    if found is None:
        return
    prev = _load_cash_close(conn, key)
    _CASH_CLOSE[key] = float(found)
    _save_cash_close(conn, key, float(found))
    if prev is not None and float(prev) != float(found):
        log.info("cash close %s %.2f → %.2f", key, float(prev), float(found))


def _load_cash_close(conn: sqlite3.Connection, day: str) -> float | None:
    row = conn.execute(
        "SELECT cash_close, cash_close_day FROM desk_state WHERE player_id = ?",
        (_pid(),),
    ).fetchone()
    if row is None or row["cash_close"] is None or row["cash_close_day"] != day:
        return None
    return float(row["cash_close"])


def _save_cash_close(conn: sqlite3.Connection, day: str, value: float) -> None:
    if _load_cash_close(conn, day) == value:
        return
    _ensure_state(conn)
    conn.execute(
        "UPDATE desk_state SET cash_close = ?, cash_close_day = ? WHERE player_id = ?",
        (value, day, _pid()),
    )


def _cash_close(marks: DeskMarks) -> float | None:
    """SPX cash close: HTTP latch (live) or tape through 16:05 (backtest)."""
    if marks.ts is None:
        return None
    day = marks.ts.astimezone(_ET).date()
    found = _CASH_CLOSE.get(day.isoformat())
    if found is not None:
        return found
    return _tape_cash(marks, day)


def _tape_cash(marks: DeskMarks, day: date) -> float | None:
    stamp = int(datetime.combine(day, _CASH_FETCH_AT, tzinfo=_ET).timestamp())
    index = _SPX_AT.get(day.isoformat())
    if index:
        if stamp in index:
            return float(index[stamp]["value"])
        earlier = [key for key in index if key <= stamp]
        if earlier:
            return float(index[max(earlier)]["value"])
    point = None
    for item in marks.tape or []:
        if int(item["time"]) <= stamp:
            point = item
        else:
            break
    if point is None:
        return None
    return float(point["value"])
