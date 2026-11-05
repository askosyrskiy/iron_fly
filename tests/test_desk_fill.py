"""Backtest must refuse opens when a wing quote is missing (no 0.01 STO)."""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

from iron_fly.config import Settings
from iron_fly.db import connect, migrate
from iron_fly.desk import _fill_intent, _fill_modes, load_book, open_strike
from iron_fly.domain.market import ChainSnapshot, DeskMarks, StrikeQuote, combo_fill
from iron_fly.domain.models import Intent, IntentKind
from iron_fly.orders import event_rows, leftover_long_rows, park_lots, sell_ready_leftovers, short_mark
from iron_fly.strategy.dale_og import DaleOg

ET = ZoneInfo("America/New_York")


def _strike(
    strike: float,
    call: tuple[float, float],
    put: tuple[float, float],
) -> StrikeQuote:
    return StrikeQuote(strike, call[0], call[1], put[0], put[1], "", "")


def _settings(tmp_path) -> Settings:
    return Settings.from_values(
        app_base_url="http://localhost:8000",
        session_secret="test-session-secret",
        secrets_key=Fernet.generate_key().decode(),
        data_dir=tmp_path,
        desk_mode="backtest-public",
    )


def test_backtest_refuses_open_without_combo_mid_then_retries(tmp_path, caplog):
    """Missing wing quote must not book a penny STO; later tick opens when quoted."""
    thin = ChainSnapshot(
        ts=None,
        expiration=None,
        strikes=(
            _strike(7700, (10, 12), (8, 10)),
            _strike(7750, (0.4, 0.6), (0.3, 0.5)),
        ),
    )
    full = ChainSnapshot(
        ts=None,
        expiration=None,
        strikes=(
            _strike(7650, (0.2, 0.4), (0.4, 0.6)),
            _strike(7670, (0.3, 0.5), (0.5, 0.7)),
            _strike(7700, (10, 12), (8, 10)),
            _strike(7720, (9, 11), (7, 9)),
            _strike(7750, (0.4, 0.6), (0.3, 0.5)),
            _strike(7770, (0.5, 0.7), (0.4, 0.6)),
        ),
    )
    when = datetime(2026, 10, 14, 10, 50, tzinfo=ET)
    settings = _settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    marks = DeskMarks(last=7747, chain=thin, ts=when, source="replay")
    assert open_strike(conn, settings, marks, 7700, wings=50) == "nomid"
    assert conn.execute("SELECT COUNT(*) AS n FROM fly").fetchone()["n"] == 0

    strategy = DaleOg()
    strategy.book = load_book(conn)
    strategy.book.open_fly(7750, 21.0, 50, when.replace(minute=41))
    intent = Intent(IntentKind.ADD, "watch tagged", {"strike": 7700.0, "wings": 50})
    with caplog.at_level(logging.WARNING, logger="iron_fly.step"):
        assert (
            _fill_intent(conn, settings, date(2026, 10, 14), strategy, intent, when, 50, thin)
            is False
        )
    assert any("no quotes" in rec.message and "retry" in rec.message for rec in caplog.records)
    assert intent.payload.get("credit") in (None, 0, 0.0) or "credit" not in intent.payload
    active = [fly for fly in strategy.book.flies if fly.status == "active"]
    assert [(fly.strike, fly.credit) for fly in active] == [(7750, 21.0)]

    later = Intent(IntentKind.ADD, "watch tagged", {"strike": 7720.0, "wings": 50})
    assert (
        _fill_intent(
            conn, settings, date(2026, 10, 14), strategy, later, when.replace(second=5), 50, full
        )
        is True
    )
    open_mode, _close_mode = _fill_modes(conn)
    expected = combo_fill(full, 7720, 50, side="open", mode=open_mode)
    assert float(later.payload["credit"]) == expected
    opened = [fly for fly in strategy.book.flies if fly.strike == 7720 and fly.status == "active"]
    assert len(opened) == 1
    assert float(opened[0].credit) == expected
    conn.close()


def _wing_chain() -> ChainSnapshot:
    """7640 fly, dead 7620 put, live 7660 call, and the 7660 fly's far wing."""
    return ChainSnapshot(
        ts=None,
        expiration=None,
        strikes=(
            _strike(7620, (0.10, 0.20), (0.00, 0.05)),
            _strike(7640, (11.10, 11.50), (0.60, 0.70)),
            _strike(7660, (1.05, 1.10), (3.20, 3.40)),
            _strike(7680, (0.10, 0.15), (12.0, 12.4)),
        ),
    )


def test_stop_sells_the_wing_with_a_bid_and_parks_the_dead_one(tmp_path):
    """A dead put parks only that put. The call bid comes off the stop debit."""
    chain = _wing_chain()
    when = datetime(2026, 10, 22, 15, 50, 13, tzinfo=ET)
    settings = _settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    strategy = DaleOg()
    strategy.book.open_fly(7640, 8.30, 20, when.replace(minute=46))
    intent = Intent(IntentKind.CLOSE, "expiration breakeven", {"strike": 7640.0})
    assert _fill_intent(conn, settings, date(2026, 10, 22), strategy, intent, when, 20, chain) is True
    # The default desk closes at ask: 11.50 call + .70 put - 1.05 live wing.
    expected = 11.15
    assert float(intent.payload["debit"]) == expected
    parked = [(row["right"], int(row["strike"])) for row in leftover_long_rows(conn)]
    assert parked == [("put", 7620)]
    assert strategy.book.flies[0].sales == [{"right": "call", "strike": 7660.0, "bid": 1.05}]

    opened = Intent(IntentKind.ADD, "watch tagged", {"strike": 7660.0, "wings": 20})
    assert (
        _fill_intent(
            conn,
            settings,
            date(2026, 10, 22),
            strategy,
            opened,
            when.replace(second=45),
            20,
            chain,
        )
        is True
    )
    assert any(fly.strike == 7660 and fly.status == "active" for fly in strategy.book.flies)
    conn.close()


def test_add_sells_a_parked_long_that_blocks_the_short(tmp_path):
    """A parked call with a bid is sold on the add print, then the fly opens."""
    chain = _wing_chain()
    when = datetime(2026, 10, 22, 15, 50, 45, tzinfo=ET)
    settings = _settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    park_lots(
        conn,
        [{"occ": "call:7660", "right": "call", "strike": 7660.0, "quantity": 1}],
    )
    strategy = DaleOg()
    strategy.book.open_fly(7640, 8.30, 20, when.replace(minute=46))
    strategy.book.close_fly(7640, 11.95, when.replace(second=13))
    intent = Intent(IntentKind.ADD, "watch tagged", {"strike": 7660.0, "wings": 20})
    assert _fill_intent(conn, settings, date(2026, 10, 22), strategy, intent, when, 20, chain) is True
    closed = next(fly for fly in strategy.book.flies if fly.strike == 7640)
    assert closed.close_debit == round(11.95 - 1.05, 2)
    assert leftover_long_rows(conn) == []
    assert any(fly.strike == 7660 and fly.status == "active" for fly in strategy.book.flies)
    conn.close()


def test_add_stays_blocked_when_the_parked_long_has_no_bid(tmp_path):
    chain = ChainSnapshot(
        ts=None,
        expiration=None,
        strikes=(
            _strike(7640, (11.10, 11.50), (0.60, 0.70)),
            _strike(7660, (0.00, 0.05), (3.20, 3.40)),
            _strike(7680, (0.10, 0.15), (12.0, 12.4)),
        ),
    )
    when = datetime(2026, 10, 22, 15, 50, 45, tzinfo=ET)
    settings = _settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    park_lots(
        conn,
        [{"occ": "call:7660", "right": "call", "strike": 7660.0, "quantity": 1}],
    )
    strategy = DaleOg()
    intent = Intent(IntentKind.ADD, "watch tagged", {"strike": 7660.0, "wings": 20})
    assert _fill_intent(conn, settings, date(2026, 10, 22), strategy, intent, when, 20, chain) is False
    assert [(row["right"], int(row["strike"])) for row in leftover_long_rows(conn)] == [("call", 7660)]
    assert strategy.book.active() == []
    assert any(row["text"].startswith("Open blocked 7660 @ 20") for row in event_rows(conn))
    conn.close()


def test_add_logs_when_short_fights_an_open_fly_wing(tmp_path):
    from iron_fly.orders import event_rows, load_open_conflict

    chain = _wing_chain()
    when = datetime(2026, 10, 22, 15, 50, 45, tzinfo=ET)
    settings = _settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    strategy = DaleOg()
    strategy.book.open_fly(7640, 8.30, 20, when.replace(minute=46))
    intent = Intent(IntentKind.ADD, "watch tagged", {"strike": 7660.0, "wings": 20})
    assert _fill_intent(conn, settings, date(2026, 10, 22), strategy, intent, when, 20, chain) is False
    assert len(strategy.book.active()) == 1
    banner = load_open_conflict(conn)
    assert banner is not None
    assert banner.startswith("Open blocked 7660 @ 20")
    assert "short 7660 call" in banner
    assert any(row["text"] == banner for row in event_rows(conn))
    conn.close()


def test_add_widens_the_call_that_lands_on_a_short(tmp_path):
    """7620 @ 20 wants the 7640 call that is already short. The call steps out to 7645."""
    from iron_fly.desk import save_book
    from iron_fly.orders import fly_spans

    chain = ChainSnapshot(
        ts=None,
        expiration=None,
        strikes=(
            _strike(7600, (0.20, 0.30), (1.20, 1.40)),
            _strike(7620, (8.00, 8.40), (7.00, 7.40)),
            _strike(7640, (11.00, 11.40), (6.00, 6.40)),
            _strike(7645, (0.40, 0.60), (0.20, 0.40)),
            _strike(7690, (0.05, 0.15), (3.00, 3.20)),
        ),
    )
    when = datetime(2026, 10, 23, 15, 52, 19, tzinfo=ET)
    settings = _settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    strategy = DaleOg()
    strategy.book.open_fly(7640, 20.0, 50, when.replace(second=0))
    intent = Intent(IntentKind.ADD, "watch tagged", {"strike": 7620.0, "wings": 20})
    assert _fill_intent(conn, settings, date(2026, 10, 23), strategy, intent, when, 20, chain) is True
    opened = next(fly for fly in strategy.book.active() if fly.strike == 7620)
    assert fly_spans(opened) == (20, 25)
    open_mode, _close_mode = _fill_modes(conn)
    assert float(intent.payload["credit"]) == combo_fill(
        chain, 7620, 20, side="open", mode=open_mode, call_wings=25
    )
    texts = [row["text"] for row in event_rows(conn)]
    assert any("7600/7620/7645" in text for text in texts)
    assert not any(text.startswith("Open blocked") for text in texts)
    save_book(conn, strategy.book)
    loaded = next(fly for fly in load_book(conn).active() if fly.strike == 7620)
    assert fly_spans(loaded) == (20, 25)
    conn.close()


def test_add_stays_blocked_when_one_step_still_hits_a_short(tmp_path):
    when = datetime(2026, 10, 23, 15, 52, 19, tzinfo=ET)
    settings = _settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    strategy = DaleOg()
    strategy.book.open_fly(7640, 20.0, 50, when)
    strategy.book.open_fly(7645, 20.0, 50, when)
    intent = Intent(IntentKind.ADD, "watch tagged", {"strike": 7620.0, "wings": 20})
    assert _fill_intent(conn, settings, date(2026, 10, 23), strategy, intent, when, 20, _wing_chain()) is False
    assert [fly.strike for fly in strategy.book.active()] == [7640, 7645]
    assert any(row["text"].startswith("Open blocked 7620 @ 20") for row in event_rows(conn))
    conn.close()


def test_widen_steps_the_put_and_leaves_a_short_overlap_blocked():
    from iron_fly.orders import widen_blocked_open

    when = datetime(2026, 10, 23, 15, 52, tzinfo=ET)
    call_book = DaleOg()
    call_book.book.open_fly(7640, 20.0, 50, when)
    assert widen_blocked_open(call_book.book.active(), 7620, 20) == (20, 25)
    put_book = DaleOg()
    put_book.book.open_fly(7600, 20.0, 50, when)
    assert widen_blocked_open(put_book.book.active(), 7620, 20) == (25, 20)
    body = DaleOg()
    body.book.open_fly(7640, 8.30, 20, when)
    assert widen_blocked_open(body.book.active(), 7660, 20) is None


def test_widen_steps_call_twice_when_two_shorts_block():
    from iron_fly.orders import widen_blocked_open

    when = datetime(2026, 11, 4, 15, 54, tzinfo=ET)
    book = DaleOg()
    book.book.open_fly(7705, 24.0, 50, when)
    book.book.open_fly(7710, 20.0, 50, when)
    assert widen_blocked_open(book.book.active(), 7685, 20) == (20, 30)


def test_widen_stops_at_fifty():
    from iron_fly.orders import WIDEN_MAX_WING, WIDEN_STEP, widen_blocked_open

    when = datetime(2026, 11, 4, 15, 54, tzinfo=ET)
    book = DaleOg()
    for center in range(7620, 7620 + WIDEN_MAX_WING, WIDEN_STEP):
        book.book.open_fly(float(center), 20.0, 50, when)
    assert widen_blocked_open(book.book.active(), 7600, 20) is None


def test_open_blocked_suggests_wider_wings_when_that_clears_the_book(tmp_path):
    from iron_fly.orders import load_open_conflict, open_book_hold, record_open_blocked

    when = datetime(2026, 10, 22, 15, 50, tzinfo=ET)
    settings = _settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    strategy = DaleOg()
    strategy.book.open_fly(7700, 12.0, 25, when)
    assert open_book_hold(strategy.book.active(), 7680, 20) is not None
    assert open_book_hold(strategy.book.active(), 7680, 25) is None
    text = record_open_blocked(
        conn,
        when,
        7680,
        20,
        open_book_hold(strategy.book.active(), 7680, 20) or "",
        strategy.book.active(),
    )
    assert "Try width 25" in text
    assert load_open_conflict(conn) == text
    conn.close()


def test_a_rich_parked_long_rolls_and_remembers_where_it_came_from(tmp_path):
    chain = ChainSnapshot(
        ts=None,
        expiration=None,
        strikes=(
            _strike(7660, (1.10, 1.30), (0.05, 0.10)),
            _strike(7685, (0.35, 0.45), (0.05, 0.10)),
        ),
    )
    when = datetime(2026, 10, 22, 15, 48, tzinfo=ET)
    settings = _settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    park_lots(conn, [{"occ": "call:7660", "right": "call", "strike": 7660.0, "quantity": 1}])
    assert sell_ready_leftovers(conn, settings, chain, None, when) == 1
    parked = leftover_long_rows(conn)
    assert [(row["right"], int(row["strike"]), int(row["from_strike"])) for row in parked] == [("call", 7685, 7660)]
    roll = conn.execute("SELECT right, from_strike, to_strike, mark FROM leftover_roll").fetchone()
    assert (roll["right"], int(roll["from_strike"]), int(roll["to_strike"]), float(roll["mark"])) == (
        "call",
        7660,
        7685,
        1.2,
    )
    conn.close()


def test_regime_stop_and_wing_changes_are_events(tmp_path):
    from iron_fly.orders import event_rows, forget_policy, note_policy

    when = datetime(2026, 10, 22, 15, 45, 2, tzinfo=ET)
    settings = _settings(tmp_path)
    conn = connect(settings.db_path)
    migrate(conn)
    forget_policy()
    note_policy(conn, when, "ladder", 3, "20")
    note_policy(conn, when.replace(second=3), "ladder", 3, "20")
    later = when.replace(minute=50, second=13)
    note_policy(conn, later, "cookies", 5, "15")
    texts = [row["text"] for row in event_rows(conn)]
    assert texts == ["Regime Cookies ±2.5 / 5", "Stop +5", "Wings 15"]
    conn.close()
