from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from iron_fly import desk
from iron_fly.config import Settings
from iron_fly.db import connect, migrate
from iron_fly.domain.market import ChainSnapshot, DeskMarks, StrikeQuote
from iron_fly.domain.models import Intent, IntentKind
from iron_fly.strategy.dale_og import DaleOg
from iron_fly.strategy.spec import parse_spec

WHEN = datetime(2026, 10, 20, 10, 40, tzinfo=ZoneInfo("America/New_York"))


def chain(at, extra=0):
    return ChainSnapshot(
        at,
        at.date(),
        (
            StrikeQuote(7650, 0.2, 0.4, 0.4, 0.6),
            StrikeQuote(7700, 10 + extra, 12 + extra, 8, 10),
            StrikeQuote(7750, 0.4, 0.6, 0.3, 0.5),
        ),
    )


@pytest.fixture
def session(tmp_path):
    settings = Settings.from_values(
        app_base_url="http://localhost:8000", session_secret="test", secrets_key="", data_dir=tmp_path
    )
    conn = connect(settings.db_path)
    migrate(conn)
    yield conn, settings
    conn.close()


@pytest.mark.parametrize("delay, fill_second", [(0, 1), (250, 2), (2000, 3)])
@pytest.mark.parametrize("use_config", [True, False], ids=["use-config", "manual-auto-drop"])
def test_replay_close_uses_due_ticks_quotes_after_price_recovers(
    session, monkeypatch, delay, fill_second, use_config
):
    conn, settings = session
    desk.apply_spec(
        conn,
        parse_spec(
            'f1_at: "10:40"\nwings: 50\nregime: "±20/25"\n'
            f"fill_mode_open: bid\nfill_mode_close: ask\nclose_fill_ms: {delay}\n"
        ),
    )
    desk.set_switches(conn, use_config, use_config, True)
    if not use_config:
        # Auto Drop acts on a clicked fly while the YAML still owns fill pricing.
        book = desk.load_book(conn)
        book.open_fly(7700, 18, 50, WHEN)
        desk.save_book(conn, book)
        desk._set_applied(conn, WHEN)
    # t=1 breaches the stop. At t=2 SPX recovers; the accepted close still fills.
    spots = (7700, 7720, 7700, 7700)
    tape = [{"time": int(WHEN.timestamp()) + i, "value": spot} for i, spot in enumerate(spots)]
    monkeypatch.setattr(desk, "load_day_tape", lambda *_: DeskMarks(tape=tape))
    monkeypatch.setattr(desk, "_spx_slice", lambda *_: None)
    requested = []

    def quotes(_settings, _day, at):
        requested.append(at)
        return chain(at, extra=(at - WHEN).total_seconds())

    monkeypatch.setattr(desk, "chain_asof", quotes)
    trigger = WHEN + timedelta(seconds=1)
    desk.replay_until(conn, settings, WHEN.date(), trigger)
    assert all(at <= trigger for at in requested)
    assert desk.load_book(conn).flies[0].status == ("closed" if delay == 0 else "active")
    conn.commit()
    if delay:
        # Reloading both the book and replay cursor must not lose the close.
        desk.replay_until(conn, settings, WHEN.date(), WHEN + timedelta(seconds=fill_second))
    fly = desk.load_book(conn).flies[0]
    assert fly.closed_at == WHEN + timedelta(seconds=fill_second)
    assert requested[-1] == fly.closed_at
    assert fly.close_debit == pytest.approx(21.2 + fill_second)
    assert conn.execute("SELECT COUNT(*) FROM replay_pending_close").fetchone()[0] == 0


def test_missing_fill_quote_retries_without_postponing_deadline(session, monkeypatch):
    conn, settings = session
    strategy = DaleOg()
    strategy.book.open_fly(7700, 18, 50, WHEN)
    intent = Intent(IntentKind.CLOSE, "stop", {"strike": 7700})
    monkeypatch.setattr(desk, "chain_asof", lambda *_: None)
    desk._dispatch_replay_intent(conn, settings, WHEN.date(), strategy, intent, WHEN, 50, 1000)
    desk._dispatch_replay_intent(conn, settings, WHEN.date(), strategy, intent, WHEN, 50, 1000)
    assert conn.execute("SELECT COUNT(*) FROM replay_pending_close").fetchone()[0] == 1
    desk._fill_due_replay_closes(conn, settings, WHEN.date(), strategy, WHEN + timedelta(seconds=1), 50)
    assert strategy.book.flies[0].status == "active"
    monkeypatch.setattr(desk, "chain_asof", lambda *_: chain(WHEN))
    desk._fill_due_replay_closes(conn, settings, WHEN.date(), strategy, WHEN + timedelta(seconds=2), 50)
    assert strategy.book.flies[0].closed_at == WHEN + timedelta(seconds=2)


def test_pending_close_cannot_close_a_reopened_fly_at_same_strike(session, monkeypatch):
    conn, settings = session
    strategy = DaleOg()
    strategy.book.open_fly(7700, 18, 50, WHEN)
    intent = Intent(IntentKind.CLOSE, "stop", {"strike": 7700})
    desk._dispatch_replay_intent(conn, settings, WHEN.date(), strategy, intent, WHEN, 50, 2000)
    strategy.book.close_fly(7700, 19, WHEN + timedelta(seconds=1))
    fresh = strategy.book.open_fly(7700, 17, 50, WHEN + timedelta(seconds=1))
    monkeypatch.setattr(desk, "chain_asof", lambda *_: chain(WHEN))
    desk._fill_due_replay_closes(conn, settings, WHEN.date(), strategy, WHEN + timedelta(seconds=2), 50)
    assert fresh.status == "active"
    assert conn.execute("SELECT COUNT(*) FROM replay_pending_close").fetchone()[0] == 0


def test_pending_close_is_player_scoped_and_clear_cancels_it(session):
    conn, settings = session
    strategy = DaleOg()
    strategy.book.open_fly(7700, 18, 50, WHEN)
    intent = Intent(IntentKind.CLOSE, "stop", {"strike": 7700})
    desk._dispatch_replay_intent(conn, settings, WHEN.date(), strategy, intent, WHEN, 50, 1000)
    token = desk.bind_player("other")
    try:
        desk.clear_flies(conn)
        desk._fill_due_replay_closes(conn, settings, WHEN.date(), strategy, WHEN + timedelta(seconds=2), 50)
        assert strategy.book.flies[0].status == "active"
    finally:
        desk.reset_player(token)
    assert conn.execute("SELECT COUNT(*) FROM replay_pending_close").fetchone()[0] == 1
    desk.clear_flies(conn)
    assert conn.execute("SELECT COUNT(*) FROM replay_pending_close").fetchone()[0] == 0


def test_pending_close_past_trade_stop_expires_to_cash_settlement(session, monkeypatch):
    conn, settings = session
    strategy = DaleOg()
    strategy.book.open_fly(7700, 18, 50, WHEN)
    trigger = WHEN.replace(hour=16, minute=0, second=59)
    intent = Intent(IntentKind.CLOSE, "stop", {"strike": 7700})
    desk._dispatch_replay_intent(conn, settings, WHEN.date(), strategy, intent, trigger, 50, 5000)
    monkeypatch.setattr(desk, "chain_asof", lambda *_: pytest.fail("No post-session fill quote"))
    desk._fill_due_replay_closes(conn, settings, WHEN.date(), strategy, trigger + timedelta(seconds=5), 50)
    assert strategy.book.flies[0].status == "active"
    assert conn.execute("SELECT COUNT(*) FROM replay_pending_close").fetchone()[0] == 0


def test_explicit_delay_override_of_stock_yaml_is_preserved(session):
    conn, _settings = session
    text = desk.default_spec_yaml() + "\nclose_fill_ms: 5000\n"
    desk.apply_spec(conn, parse_spec(text))
    assert desk._saved_spec(conn).close_fill_ms == 5000


def test_flatten_queues_every_fly_and_pauses_new_opens(session, monkeypatch):
    conn, settings = session
    strategy = DaleOg()
    strategy.book.open_fly(7700, 18, 50, WHEN)
    strategy.book.open_fly(7720, 17, 50, WHEN)
    quote = ChainSnapshot(
        WHEN,
        WHEN.date(),
        chain(WHEN).strikes
        + (
            StrikeQuote(7670, 0.2, 0.4, 0.4, 0.6),
            StrikeQuote(7720, 10, 12, 8, 10),
            StrikeQuote(7770, 0.4, 0.6, 0.3, 0.5),
        ),
    )
    intent = Intent(IntentKind.CLOSE, "flatten_at", {"strike": 7700})
    desk._dispatch_replay_intent(conn, settings, WHEN.date(), strategy, intent, WHEN, 50, 1000)
    assert strategy.opens_paused
    assert conn.execute("SELECT COUNT(*) FROM replay_pending_close").fetchone()[0] == 2
    monkeypatch.setattr(desk, "chain_asof", lambda *_: quote)
    desk._fill_due_replay_closes(conn, settings, WHEN.date(), strategy, WHEN + timedelta(seconds=1), 50)
    assert not strategy.book.active()
    assert not desk.load_switches(conn)["auto_open"]
