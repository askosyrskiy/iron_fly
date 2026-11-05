"""Leftover longs and the hold check that refuses a short already held long.

A close that finds a dead wing buys back the two shorts on that same print,
sells each wing that still has a bid, and parks only the wing with no bid.
A later open sells a parked long that is its new short when that long has a
bid, then opens. A parked long with no bid still blocks that short. Matching
wing leftovers can be reused: STO the short straddle and attach the parked longs.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, time
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from iron_fly.config import Settings
from iron_fly.domain.market import ChainSnapshot, StrikeQuote, mid
from iron_fly.strategy.spec import leftover_until_clock, parse_spec

log = logging.getLogger("iron_fly.step")

DEAD_LONG_MARK = 0.05
HOLD_CONFLICT = "Same contract cannot stay long and short."
OPEN_BLOCKED_PREFIX = "Open blocked"
SUGGEST_OPEN_WINGS = 25
ROLL_ABOVE = 0.50
ROLL_TARGET = 0.40


def note_event(conn: sqlite3.Connection, when: datetime, text: str) -> None:
    """Append one trading event. The blotter stays a fly list; this is the log."""
    conn.execute(
        "INSERT INTO desk_event (player_id, happened_at, body) VALUES (?, ?, ?)",
        (_pid(), when.isoformat(), text),
    )


_POLICY: dict[str, tuple[str, str, str]] = {}
_POLICY_LOCK = threading.Lock()


def forget_policy(player: str | None = None) -> None:
    """Drop the last regime, stop, and wings so the next print is a fresh baseline."""
    with _POLICY_LOCK:
        if player is None:
            _POLICY.clear()
        else:
            _POLICY.pop(player, None)


def note_policy(conn: sqlite3.Connection, when: datetime, regime: str, stop: int, wings: str) -> None:
    """Record a regime, stop, or wing change. The first print only sets the baseline."""
    from iron_fly.strategy.rules import REGIME_LABELS

    current = (str(regime), str(int(stop)), str(wings))
    with _POLICY_LOCK:
        player = _pid()
        previous = _POLICY.get(player)
        _POLICY[player] = current
        if previous is None or previous == current:
            return
        if previous[0] != current[0]:
            note_event(conn, when, f"Regime {REGIME_LABELS.get(current[0], current[0])}")
        if previous[1] != current[1]:
            note_event(conn, when, f"Stop +{current[1]}")
        if previous[2] != current[2]:
            note_event(conn, when, f"Wings {current[2]}")


def event_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT happened_at, body FROM desk_event WHERE player_id = ? ORDER BY id",
        (_pid(),),
    ).fetchall()
    return [{"at": str(row["happened_at"]), "text": str(row["body"])} for row in rows]


def _call_width(wings: int, call_wings: int | None = None) -> int:
    return int(wings if call_wings is None else call_wings)


def fly_spans(fly) -> tuple[int, int]:
    """Put width and call width. A fly with no call width is symmetric."""
    put = int(fly.wings)
    extra = getattr(fly, "call_wings", None)
    return put, _call_width(put, None if extra is None else int(extra))


def _fly_legs(strike: float, wings: int, call_wings: int | None = None) -> str:
    center = int(round(strike))
    call = int(round(strike + _call_width(wings, call_wings)))
    return f"{int(round(strike - wings))}/{center}/{call}"


def dead_reason(bid: float | None, ask: float | None) -> str | None:
    """Why a long wing cannot ride in a 4-leg close. None when it still trades."""
    if bid is None and ask is None:
        return "no quote"
    if bid is None or bid <= 0:
        return "no bid"
    mark = mid(bid, ask)
    if mark is None or mark < DEAD_LONG_MARK:
        shown = 0.0 if mark is None else mark
        return f"mark {shown:.2f} < 0.05"
    return None


def dead_wings(
    chain: ChainSnapshot | None,
    strike: float,
    wings: int,
    call_wings: int | None = None,
) -> list[str]:
    """Labels for long wings that cannot STC. Empty when both wings still trade."""
    notes = []
    call = strike + _call_width(wings, call_wings)
    for right, wing in (("put", strike - wings), ("call", call)):
        row = _row(chain, wing)
        reason = "no quote" if row is None else dead_reason(*_side(row, right))
        if reason:
            notes.append(f"{right} {int(wing)} {reason}")
    return notes


def dead_long_banner(flies: list[dict]) -> list[dict]:
    """Rows for the blotter warning: slot, label, detail. Active flies only."""
    out: list[dict] = []
    for fly in flies:
        notes = fly.get("dead_longs") or []
        if fly.get("status") != "active" or not notes:
            continue
        strike = fly.get("strike")
        out.append(
            {
                "slot": fly.get("slot"),
                "label": int(strike) if strike is not None else fly.get("label"),
                "detail": ", ".join(str(item) for item in notes),
            }
        )
    return out


def short_mark(chain: ChainSnapshot | None, strike: float, *, mode: str = "mid") -> float | None:
    """Short-straddle price at the requested side; midpoint remains the default."""
    row = _row(chain, strike)
    if row is None:
        return None
    if mode == "ask":
        if row.call_ask is None or row.put_ask is None or row.call_ask <= 0 or row.put_ask <= 0:
            return None
        return round(row.call_ask + row.put_ask, 2)
    call = mid(row.call_bid, row.call_ask)
    put = mid(row.put_bid, row.put_ask)
    if call is None or put is None:
        return None
    return round(call + put, 2)


def wing_lots(
    chain: ChainSnapshot | None,
    strike: float,
    wings: int,
    call_wings: int | None = None,
) -> list[dict]:
    """The two long wings, one row per OCC."""
    lots = []
    call = strike + _call_width(wings, call_wings)
    for right, wing in (("put", strike - wings), ("call", call)):
        row = _row(chain, wing)
        lots.append(
            {
                "occ": _occ(row, right, wing),
                "right": right,
                "strike": float(wing),
                "quantity": 1,
            }
        )
    return lots


def park_wings(
    conn: sqlite3.Connection,
    chain: ChainSnapshot | None,
    strike: float,
    wings: int,
    call_wings: int | None = None,
) -> list[dict]:
    """Remember the wings a shorts-only close left behind. One row per OCC."""
    return park_lots(conn, wing_lots(chain, strike, wings, call_wings))


def live_wing_bids(
    chain: ChainSnapshot | None,
    strike: float,
    wings: int,
    call_wings: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """Wings to sell at the bid, and wings to park.

    A bid above zero sells on the stop print with the shorts. A wing with no
    bid stays parked.
    """
    sell: list[dict] = []
    park: list[dict] = []
    for lot in wing_lots(chain, strike, wings, call_wings):
        bid = _positive_bid(chain, str(lot["right"]), float(lot["strike"]))
        if bid is None:
            park.append(lot)
            continue
        sold = dict(lot)
        sold["bid"] = bid
        sell.append(sold)
    return sell, park


def leftovers_sell_allowed(wall: time, auto: dict[str, object] | None = None) -> bool:
    """True while parked longs may still be sold or rolled."""
    return wall < leftover_until_clock(auto)


def reusable_wing_lots(
    conn: sqlite3.Connection,
    chain: ChainSnapshot | None,
    strike: float,
    wings: int,
    call_wings: int | None = None,
) -> list[dict] | None:
    """Both parked wing longs for this fly, or None if either is missing."""
    parked = leftover_long_rows(conn)
    if not parked:
        return None
    by_key = {(str(lot["right"]), int(lot["strike"])): lot for lot in parked}
    by_occ = {str(lot["occ"]): lot for lot in parked}
    matched: list[dict] = []
    for need in wing_lots(chain, strike, wings, call_wings):
        lot = by_key.get((str(need["right"]), int(need["strike"])))
        if lot is None:
            lot = by_occ.get(str(need["occ"]))
        if lot is None or int(lot["quantity"]) < 1:
            return None
        matched.append(lot)
    return matched if len(matched) == 2 else None


def consume_wing_lots(conn: sqlite3.Connection, lots: list[dict]) -> None:
    """Remove one contract from each reused leftover row."""
    pid = _pid()
    for lot in lots:
        occ = str(lot["occ"])
        qty = int(lot["quantity"]) - 1
        if qty <= 0:
            conn.execute(
                "DELETE FROM leftover_long WHERE player_id = ? AND occ = ?",
                (pid, occ),
            )
        else:
            conn.execute(
                "UPDATE leftover_long SET quantity = ? WHERE player_id = ? AND occ = ?",
                (qty, pid, occ),
            )


def park_lots(conn: sqlite3.Connection, lots: list[dict]) -> list[dict]:
    """Remember longs the wing order did not take. One row per OCC."""
    pid = _pid()
    for lot in lots:
        conn.execute(
            """
            INSERT INTO leftover_long (player_id, occ, right, strike, quantity)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(player_id, occ) DO UPDATE SET
                right = excluded.right,
                strike = excluded.strike,
                quantity = excluded.quantity
            """,
            (pid, lot["occ"], lot["right"], float(lot["strike"]), int(lot["quantity"])),
        )
    return lots


def leftover_long_rows(conn: sqlite3.Connection) -> list[dict]:
    """Parked longs. Empty when every long is still attached to a fly."""
    rows = conn.execute(
        """
        SELECT occ, right, strike, quantity, from_strike FROM leftover_long
        WHERE player_id = ?
        ORDER BY strike, right
        """,
        (_pid(),),
    ).fetchall()
    return [
        {
            "occ": str(row["occ"]),
            "right": str(row["right"]),
            "strike": float(row["strike"]),
            "quantity": int(row["quantity"]),
            "from_strike": None if row["from_strike"] is None else float(row["from_strike"]),
        }
        for row in rows
    ]


def leftover_long_note(lots: list[dict]) -> str | None:
    if not lots:
        return None
    parts = [f"{lot['right']} {int(lot['strike'])}" for lot in lots]
    return "leftover longs " + ", ".join(parts)


def leftover_long_panel(conn: sqlite3.Connection, chain: ChainSnapshot | None) -> list[dict]:
    """One row per parked OCC, with the latest bid, ask, and mid."""
    out = []
    for lot in leftover_long_rows(conn):
        row = _row(chain, float(lot["strike"]))
        bid, ask = (None, None) if row is None else _side(row, str(lot["right"]))
        mark = mid(bid, ask)
        reason = dead_reason(bid, ask)
        out.append(
            {
                "right": lot["right"],
                "strike": lot["strike"],
                "occ": lot["occ"],
                "quantity": lot["quantity"],
                "bid": bid,
                "ask": ask,
                "mid": None if mark is None else round(mark, 2),
                "from_strike": lot.get("from_strike"),
                "dead": reason is not None,
                "dead_reason": reason,
            }
        )
    return out


def _positive_bid(chain: ChainSnapshot | None, right: str, strike: float) -> float | None:
    row = _row(chain, strike)
    if row is None:
        return None
    bid, _ask = _side(row, right)
    if bid is None or bid <= 0:
        return None
    return float(bid)


def _conflicting_lots(conn: sqlite3.Connection, chain: ChainSnapshot | None, strike: float) -> list[dict]:
    """Parked longs whose contract is the call or put at this short."""
    parked = leftover_long_rows(conn)
    if not parked:
        return []
    by_key = {_key(str(lot["right"]), float(lot["strike"])): lot for lot in parked}
    by_occ = {str(lot["occ"]): lot for lot in parked}
    hits: list[dict] = []
    seen: set[str] = set()
    for right in ("call", "put"):
        row = _row(chain, strike)
        occ = _occ(row, right, strike)
        lot = by_occ.get(occ) or by_key.get(_key(right, float(strike)))
        if lot is None:
            continue
        ident = str(lot["occ"])
        if ident in seen:
            continue
        seen.add(ident)
        hits.append(lot)
    return hits


def open_book_hold(active, strike: float, wings: int, call_wings: int | None = None) -> str | None:
    """Refuse when a new fly would short an open fly's wing or long a body short."""
    body = int(round(strike))
    put_wing = int(round(strike - wings))
    call_wing = int(round(strike + _call_width(wings, call_wings)))
    parts: list[str] = []
    for fly in active:
        center = int(round(fly.strike))
        put_span, call_span = fly_spans(fly)
        if body == center - put_span:
            parts.append(f"short {body} put vs long put on {center}/{put_span}")
        if body == center + call_span:
            parts.append(f"short {body} call vs long call on {center}/{call_span}")
        if put_wing == center:
            parts.append(f"long put {put_wing} vs short put on {center}")
        if call_wing == center:
            parts.append(f"long call {call_wing} vs short call on {center}")
    if not parts:
        return None
    return "; ".join(parts) + ". " + HOLD_CONFLICT


WIDEN_STEP = 5
WIDEN_MAX_WING = 50


def _bump_wing(current: int) -> int | None:
    """Step width out by 5, capped at 50. None when already at the max."""
    if current >= WIDEN_MAX_WING:
        return None
    return min(current + WIDEN_STEP, WIDEN_MAX_WING)


def widen_blocked_open(active, strike: float, wings: int) -> tuple[int, int] | None:
    """Put and call widths for this open.

    A long wing that lands on an open short steps out by 5, repeatedly until
    the book accepts the fly or a non-wing overlap remains. Each side caps at
    50 (20, 25, 30, …). None when no step helps, at the cap, or when the new
    short body still fights the book.
    """
    width = int(wings)
    if open_book_hold(active, strike, width) is None:
        return width, width
    put_w, call_w = width, width
    while True:
        if open_book_hold(active, strike, put_w, call_w) is None:
            return put_w, call_w
        moved = False
        if _long_hits_short(active, strike - put_w):
            next_put = _bump_wing(put_w)
            if next_put is None:
                return None
            put_w = next_put
            moved = True
        if _long_hits_short(active, strike + call_w):
            next_call = _bump_wing(call_w)
            if next_call is None:
                return None
            call_w = next_call
            moved = True
        if not moved:
            return None


def _long_hits_short(active, wing: float) -> bool:
    strike = int(round(wing))
    return any(int(round(fly.strike)) == strike for fly in active)


def clear_open_conflict(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE desk_state SET open_conflict = NULL WHERE player_id = ?", (_pid(),))


def load_open_conflict(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT open_conflict FROM desk_state WHERE player_id = ?",
        (_pid(),),
    ).fetchone()
    if row is None or row["open_conflict"] is None:
        return None
    text = str(row["open_conflict"]).strip()
    return text or None


def record_open_blocked(
    conn: sqlite3.Connection,
    when: datetime,
    strike: float,
    wings: int,
    reason: str,
    active,
) -> str:
    """Log a skipped open and latch the banner until the next successful open."""
    hint = ""
    if (
        wings != SUGGEST_OPEN_WINGS
        and open_book_hold(active, strike, SUGGEST_OPEN_WINGS) is None
    ):
        hint = f" Try width {SUGGEST_OPEN_WINGS} in Open fly."
    body = f"{OPEN_BLOCKED_PREFIX} {int(strike)} @ {wings}: {reason}{hint}"
    note_event(conn, when, body)
    conn.execute(
        "INSERT INTO desk_state (player_id, regime, stop_pad, use_config) VALUES (?, 's3', 0, 1) "
        "ON CONFLICT(player_id) DO NOTHING",
        (_pid(),),
    )
    conn.execute(
        "UPDATE desk_state SET open_conflict = ? WHERE player_id = ?",
        (body, _pid()),
    )
    return body


def hold_conflict(conn: sqlite3.Connection, chain: ChainSnapshot | None, strike: float) -> str | None:
    """Refuse an open whose new short is already a leftover long.

    A wing parked at another strike does not block the open.
    """
    lots = _conflicting_lots(conn, chain, strike)
    if not lots:
        return None
    hits = [f"long {int(strike)} {lot['right']}" for lot in lots]
    return "This fly would fight leftover longs (" + ", ".join(hits) + "). " + HOLD_CONFLICT


def priced_short_conflicts(
    conn: sqlite3.Connection,
    chain: ChainSnapshot | None,
    strike: float,
) -> tuple[str | None, list[dict]]:
    """Parked longs that are this short, each priced at its bid.

    When any of those longs has no bid, the conflict message is set and the
    sale list is empty. The caller leaves every row parked.
    """
    lots = _conflicting_lots(conn, chain, strike)
    if not lots:
        return None, []
    priced: list[dict] = []
    for lot in lots:
        bid = _positive_bid(chain, str(lot["right"]), float(lot["strike"]))
        if bid is None:
            return hold_conflict(conn, chain, strike), []
        sold = dict(lot)
        sold["bid"] = bid
        priced.append(sold)
    return None, priced


def release_longs(conn: sqlite3.Connection, lots: list[dict]) -> None:
    """Drop parked rows that were sold on this print."""
    pid = _pid()
    for lot in lots:
        conn.execute(
            "DELETE FROM leftover_long WHERE player_id = ? AND occ = ?",
            (pid, str(lot["occ"])),
        )


def sell_ready_leftovers(
    conn: sqlite3.Connection,
    settings: Settings,
    chain: ChainSnapshot | None,
    auto: dict[str, object] | None = None,
    when: datetime | None = None,
) -> int:
    """Sell a cheap parked long. Roll one above roll_above to the roll_to strike.

    The put and the call are separate. The wing already at roll_to stays parked.
    """
    above = _threshold(auto, "roll_above", ROLL_ABOVE)
    target_price = _threshold(auto, "roll_to", ROLL_TARGET)
    done = 0
    for lot in leftover_long_rows(conn):
        right = str(lot["right"])
        strike = float(lot["strike"])
        row = _row(chain, strike)
        bid, ask = (None, None) if row is None else _side(row, right)
        value = _value(bid, ask)
        anchor = _forty_strike(chain, right, target=target_price)
        if anchor is not None and int(anchor) == int(strike):
            continue
        if value is not None and value > above:
            target = _forty_strike(chain, right, beyond=strike, target=target_price)
            if target is not None and _roll_leftover(conn, settings, chain, lot, target, when, value):
                done += 1
                continue
        if bid is None or bid <= 0:
            continue
        if close_leftover_long(conn, settings, chain, str(lot["occ"]), when) is None:
            done += 1
    return done


def _value(bid: float | None, ask: float | None) -> float | None:
    mark = mid(bid, ask)
    if mark is not None:
        return mark
    if bid is not None and bid > 0:
        return bid
    return None


def _threshold(auto: dict[str, object] | None, key: str, default: float) -> float:
    raw = (auto or {}).get(key)
    if raw in (None, ""):
        return default
    return float(raw)


def _forty_strike(
    chain: ChainSnapshot | None,
    right: str,
    *,
    beyond: float | None = None,
    target: float = ROLL_TARGET,
) -> float | None:
    """Strike of this right whose mid is nearest `target`. `beyond` stays further out."""
    if chain is None:
        return None
    marks: list[tuple[float, float]] = []
    for row in chain.strikes:
        mark = _value(*_side(row, right))
        if mark is None or mark <= 0:
            continue
        if beyond is not None and right == "put" and row.strike >= beyond:
            continue
        if beyond is not None and right == "call" and row.strike <= beyond:
            continue
        marks.append((row.strike, mark))
    if not marks:
        return None
    return min(marks, key=lambda item: (abs(item[1] - target), abs(item[0] - (beyond or item[0]))))[0]


def _roll_leftover(
    conn: sqlite3.Connection,
    settings: Settings,
    chain: ChainSnapshot | None,
    lot: dict,
    target: float,
    when: datetime | None = None,
    mark: float | None = None,
) -> bool:
    """STC this long and BTO the further strike. The new long stays parked."""
    right = str(lot["right"])
    row = _row(chain, target)
    opened = _occ(row, right, target)
    closed = str(lot["occ"])
    if opened == closed:
        return False
    held = next((item for item in leftover_long_rows(conn) if item["occ"] == closed), None)
    if held is None:
        return False
    old = _row(chain, float(held["strike"]))
    bid = None if old is None else _side(old, right)[0]
    ask = None if row is None else _side(row, right)[1]
    if bid is None or bid <= 0 or ask is None or ask <= 0:
        return False
    quantity = int(held["quantity"])
    origin = float(lot["strike"])
    with _parked_transaction(conn):
        _replace_leftover(conn, closed, opened, right, target, quantity, origin)
        _record_parked_cash(conn, when, "roll", closed, quantity, bid - ask, 2)
    stored = None if mark is None else round(float(mark), 2)
    conn.execute(
        """
        INSERT INTO leftover_roll (player_id, rolled_at, right, from_strike, to_strike, mark)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (_pid(), (when or datetime.now()).isoformat(), right, origin, float(target), stored),
    )
    price = "" if stored is None else f" {stored:.2f}"
    note_event(conn, when or datetime.now(), f"Rolled long {right} {int(origin)}{price} → {int(target)}")
    log.info("step rolled leftover %s %s to %s", right, int(origin), int(target))
    return True


def _replace_leftover(
    conn: sqlite3.Connection,
    closed: str,
    opened: str,
    right: str,
    strike: float,
    quantity: int,
    from_strike: float | None = None,
) -> None:
    pid = _pid()
    conn.execute(
        "DELETE FROM leftover_long WHERE player_id = ? AND occ = ?",
        (pid, closed),
    )
    conn.execute(
        """
        INSERT INTO leftover_long (player_id, occ, right, strike, quantity, from_strike)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(player_id, occ) DO UPDATE SET
            quantity = leftover_long.quantity + excluded.quantity,
            from_strike = excluded.from_strike
        """,
        (pid, opened, right, strike, quantity, from_strike),
    )


def close_leftover_long(
    conn: sqlite3.Connection,
    settings: Settings,
    chain: ChainSnapshot | None,
    occ: str,
    when: datetime | None = None,
) -> str | None:
    """STC one parked long at the bid from the replay chain."""
    lot = next((row for row in leftover_long_rows(conn) if row["occ"] == occ), None)
    if lot is None:
        return "missing"
    row = _row(chain, float(lot["strike"]))
    bid, _ask = (None, None) if row is None else _side(row, str(lot["right"]))
    if bid is None or bid <= 0:
        return "nobid"
    with _parked_transaction(conn):
        deleted = conn.execute(
            "DELETE FROM leftover_long WHERE player_id = ? AND occ = ?",
            (_pid(), occ),
        ).rowcount
        if not deleted:
            return "missing"
        _record_parked_cash(conn, when, "sale", occ, int(lot["quantity"]), bid, 1)
    note_event(
        conn,
        when or datetime.now(),
        f"Closed long {lot['right']} {int(lot['strike'])} at {float(bid):.2f}",
    )
    log.info("step sold leftover long %s %s", lot["right"], int(lot["strike"]))
    return None


@contextmanager
def _parked_transaction(conn: sqlite3.Connection):
    """Keep inventory and its cash entry together without committing the caller's book."""
    conn.execute("SAVEPOINT parked_inventory_cash")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK TO parked_inventory_cash")
        conn.execute("RELEASE parked_inventory_cash")
        raise
    else:
        conn.execute("RELEASE parked_inventory_cash")


def _record_parked_cash(conn, when, kind, occ, quantity, points, legs):
    clock = when or datetime.now(ZoneInfo("America/New_York"))
    day = clock.astimezone(ZoneInfo("America/New_York")).date() if clock.tzinfo else clock.date()
    state = conn.execute("SELECT spec_yaml FROM desk_state WHERE player_id = ?", (_pid(),)).fetchone()
    fee = Decimal(0)
    if state is not None and state["spec_yaml"]:
        fee = Decimal(str(parse_spec(state["spec_yaml"]).fee_per_contract))
    # Option points -> dollars -> integer dollar cents; preserve sub-point fees.
    dollars = Decimal(str(points)) * quantity * 100 - fee * quantity * legs
    cents = int((dollars * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    conn.execute(
        "INSERT INTO desk_state (player_id, book_day) VALUES (?, ?) "
        "ON CONFLICT(player_id) DO UPDATE SET book_day = COALESCE(book_day, excluded.book_day)",
        (_pid(), day.isoformat()),
    )
    conn.execute(
        "INSERT INTO parked_cashflow "
        "(player_id, session_day, happened_at, kind, occ, quantity, amount_usd_cents) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_pid(), day.isoformat(), clock.isoformat(), kind, occ, quantity, cents),
    )


def parked_cash_points(conn: sqlite3.Connection, session_day: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_usd_cents), 0) FROM parked_cashflow "
        "WHERE player_id = ? AND session_day = ?", (_pid(), session_day),
    ).fetchone()
    return int(row[0]) / 10_000


def settle_parked_longs(conn: sqlite3.Connection, session_day: str, cash_close: float) -> None:
    """Retain known expiry proceeds in the dated ledger before inventory is archived."""
    clock = datetime.fromisoformat(f"{session_day}T16:00:00").replace(tzinfo=ZoneInfo("America/New_York"))
    for lot in leftover_long_rows(conn):
        intrinsic = cash_close - float(lot["strike"])
        points = max(0.0, intrinsic if lot["right"] == "call" else -intrinsic)
        _record_parked_cash(conn, clock, "settlement", lot["occ"], int(lot["quantity"]), points, 0)


def parked_long_value(conn: sqlite3.Connection, chain, cash_close: float | None = None) -> float | None:
    value = 0.0
    for lot in leftover_long_rows(conn):
        if cash_close is not None:
            intrinsic = cash_close - float(lot["strike"])
            mark = max(0.0, intrinsic if lot["right"] == "call" else -intrinsic)
        else:
            row = _row(chain, float(lot["strike"]))
            mark = None if row is None else mid(*_side(row, str(lot["right"])))
        if mark is None:
            return None
        value += mark * int(lot["quantity"])
    return round(value, 4)


def _pid() -> str:
    from iron_fly.desk import _pid as player

    return player()


def _row(chain: ChainSnapshot | None, strike: float) -> StrikeQuote | None:
    if chain is None:
        return None
    for row in chain.strikes:
        if row.strike == strike:
            return row
    return None


def _side(row: StrikeQuote, right: str) -> tuple[float | None, float | None]:
    if right == "call":
        return row.call_bid, row.call_ask
    return row.put_bid, row.put_ask


def _occ(row: StrikeQuote | None, right: str, strike: float) -> str:
    if row is not None:
        symbol = row.call_symbol if right == "call" else row.put_symbol
        if symbol:
            return symbol
    return _key(right, strike)


def _key(right: str, strike: float) -> str:
    return f"{right}:{int(strike)}"
