"""Daily backtest lines include the session open EM and the EM at each fly open."""

from datetime import datetime
from zoneinfo import ZoneInfo

from iron_fly.backtest import _pnl_extremes, _range_text, _report
from iron_fly.desk import _path_pnl
from iron_fly.strategy.book import FlyBook, OpenFly

_ET = ZoneInfo("America/New_York")


def test_report_prints_open_em_and_the_reading_at_each_fly_open():
    first = datetime(2026, 9, 8, 15, 40, 2, tzinfo=_ET)
    second = datetime(2026, 9, 8, 15, 42, 10, tzinfo=_ET)
    tape = [
        {"time": int(datetime(2026, 9, 8, 9, 30, 2, tzinfo=_ET).timestamp()), "em": 19.12, "pct": 83.5},
        {"time": int(first.timestamp()), "em": 5.82, "pct": 25.4},
        {"time": int(second.timestamp()), "em": 5.5, "pct": 24.0},
    ]
    frame = {
        "em_open": 22.9,
        "pnl": -9.18,
        "flies": [
            {
                "time_open": "15:40",
                "time_close": "15:50",
                "strike": 7685,
                "wings": 20,
                "status": "closed",
                "sto": 5.45,
                "pnl": -3.89,
                "sales": [{"right": "call", "strike": 7705, "bid": 1.05}],
            },
            {
                "time_open": "15:42",
                "time_close": "15:54",
                "strike": 7680,
                "wings": 20,
                "status": "closed",
                "sto": 5.80,
                "pnl": -3.75,
            },
        ],
    }
    text = _report(
        first.date(),
        "Desk auto",
        frame,
        settled=True,
        tape=tape,
        opened=[first, second],
    )
    assert text.splitlines()[0].endswith("open EM 22.9")
    assert "em   5.82  em%  25.4  pnl -389" in text
    assert "sold call" not in text.split("Events")[0]
    assert "em    5.5  em%    24  pnl -375" in text


def test_report_prints_events_after_the_blotter():
    when = datetime(2026, 10, 22, 15, 48, tzinfo=_ET)
    text = _report(
        when.date(),
        "Desk auto",
        {"em_open": 20.0, "pnl": 1.0, "flies": []},
        settled=True,
        events=[
            {"at": when.isoformat(), "text": "Opened fly 7665 (7645/7665/7685) at 21.75"},
            {"at": when.isoformat(), "text": "Closed straddle 7665"},
            {"at": when.isoformat(), "text": "Parked longs 7645 7685"},
            {"at": when.isoformat(), "text": "Rolled long call 7660 1.20 → 7685"},
        ],
    )
    assert text.index("P&L") < text.index("Events")
    assert "15:48:00 Opened fly 7665 (7645/7665/7685) at 21.75" in text
    assert "15:48:00 Closed straddle 7665" in text
    assert "15:48:00 Parked longs 7645 7685" in text
    assert "15:48:00 Rolled long call 7660 1.20 → 7685" in text


def test_report_prints_the_session_pnl_low_and_high():
    when = datetime(2026, 10, 22, 15, 48, tzinfo=_ET)
    text = _report(
        when.date(),
        "Desk auto",
        {"em_open": 20.0, "pnl": 1.0, "flies": []},
        settled=True,
        pnl_span="min -480 at 10:52:24  max +2,100 at 15:12:01",
    )
    assert "P&L +100  min -480 at 10:52:24  max +2,100 at 15:12:01" in text


class _Quotes:
    def combo(self, strike, wings, call_wings=None):
        return {7400.0: 30.0, 7380.0: 8.0}[float(strike)]

    def mid(self, strike, right):
        return 0.4


def test_path_pnl_marks_open_flies_and_keeps_closed_nets():
    book = FlyBook()
    book.flies = [
        OpenFly(7400, 36.0, 50, datetime(2026, 11, 6, 10, 39, tzinfo=_ET)),
        OpenFly(
            7380,
            6.0,
            20,
            datetime(2026, 11, 6, 15, 53, tzinfo=_ET),
            status="closed",
            close_debit=4.0,
            closed_at=datetime(2026, 11, 6, 15, 58, tzinfo=_ET),
        ),
    ]
    when = datetime(2026, 11, 6, 15, 30, tzinfo=_ET)
    # Open fly is credit 36 minus mark 30. Closed fly keeps credit 6 minus debit 4.
    assert _path_pnl(book, _Quotes(), [], 0.0, when, None) == 8.0


def test_extremes_keep_the_first_time_the_low_and_high_printed():
    low = datetime(2026, 11, 6, 10, 52, 24, tzinfo=_ET)
    again = datetime(2026, 11, 6, 12, 1, tzinfo=_ET)
    high = datetime(2026, 11, 6, 15, 12, 1, tzinfo=_ET)
    path = [(low, -4.8), (again, -4.8), (high, 21.0), (datetime(2026, 11, 6, 16, 0, 1, tzinfo=_ET), 6.0)]
    assert _pnl_extremes(path) == (-4.8, low, 21.0, high)
    assert _range_text(*_pnl_extremes(path)) == "min -480 at 10:52:24  max +2,100 at 15:12:01"
