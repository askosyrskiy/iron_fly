"""Run one desk YAML over baked replay days.

    .venv/bin/python scripts/backtest.py config/default.yaml --day 2026-10-09
    .venv/bin/python scripts/backtest.py config/default.yaml --last 10
    .venv/bin/python scripts/backtest.py config/1545-20.yaml --last 30 --skip 2026-09-30,2026-10-05
    .venv/bin/python scripts/backtest.py config.yaml

The file is the same YAML as the Configuration box. A `date` field chooses
the session when `--day` is omitted. `--last N` runs the N newest files in
replay/, or every file when fewer than N are baked, and prints P&L by day.
`--skip` is a comma-separated list of YYYY-MM-DD sessions left out of that
run. Marks come from replay/.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from cryptography.fernet import Fernet

from iron_fly.config import Settings
from iron_fly.db import connect, migrate
from iron_fly.desk import apply_spec, current_player, load_day_tape, replay_until, set_switches, watch_frame
from iron_fly.quotes.replay import baked_days
from iron_fly.strategy.spec import parse_spec

_ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class DayResult:
    day: date
    report: str
    pnl: float | None
    em_open: float | None = None
    em_at_open: float | None = None
    em_pct: float | None = None
    pnl_span: str = ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest a desk YAML against baked replay days.")
    parser.add_argument("config", type=Path, help="desk YAML, the same file as the Configuration box")
    parser.add_argument("--day", action="append", default=[], help="session date YYYY-MM-DD; repeat to run several")
    parser.add_argument(
        "--last", type=int, metavar="N", help="run the N newest replay files, or every file if fewer exist"
    )
    parser.add_argument(
        "--skip",
        default="",
        metavar="DATES",
        help="comma-separated YYYY-MM-DD sessions to leave out, e.g. 2026-09-30,2026-10-05",
    )
    parser.add_argument("--replay", type=Path, help="replay directory (default: the repo replay/ folder)")
    parser.add_argument("--hydrate", action="store_true", help="download newest REPLAY_BOOTSTRAP_DAYS from R2 first")
    parser.add_argument("--verbose", action="store_true", help="print each strategy step")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    if args.last is not None and args.day:
        print("pass --day or --last, not both", file=sys.stderr)
        return 1
    if args.hydrate:
        from iron_fly.app import hydrate_replay_cache

        if args.replay is not None:
            os.environ["REPLAY_CACHE"] = str(args.replay)
        hydrate_replay_cache(required=False)
    try:
        text = args.config.read_text()
        days = _days(text, args.day, args.last, args.replay, skip=args.skip)
        if args.last is not None:
            _print_pnl(text, days, replay=args.replay)
        else:
            print("\n".join(run(text, days, replay=args.replay)))
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def run(text: str, days: list[date], *, replay: Path | None = None) -> list[str]:
    """Walk each day from the open to the last stored print and return one report per day."""
    return [item.report for item in _walk(text, days, replay=replay)]


def _walk(text: str, days: list[date], *, replay: Path | None = None):
    spec = parse_spec(text)
    if replay is not None:
        os.environ["REPLAY_CACHE"] = str(replay)
    for day in days:
        with tempfile.TemporaryDirectory() as folder:
            settings = _settings(Path(folder))
            conn = connect(settings.data_dir / "backtest.db")
            try:
                migrate(conn)
                set_switches(conn, True, True, True)
                apply_spec(conn, spec)
                conn.commit()
                end = datetime.combine(day, time(23, 59), tzinfo=_ET)
                path: list[tuple[datetime, float]] = []
                replay_until(conn, settings, day, end, pnl_path=path)
                conn.commit()
                marks = load_day_tape(settings, day)
                if marks.error or not marks.tape:
                    raise ValueError(marks.error or f"No baked tape for {day.isoformat()}")
                last = marks.tape[-1]
                marks = replace(
                    marks,
                    ts=datetime.fromtimestamp(int(last["time"]), tz=_ET),
                    last=float(last["value"]),
                )
                frame = watch_frame(conn, settings, marks)
                from iron_fly.orders import event_rows

                events = event_rows(conn)
                settled = marks.ts is not None and marks.ts.astimezone(_ET).time() >= time(16, 0)
                opened = [
                    datetime.fromisoformat(row["opened_at"])
                    for row in conn.execute(
                        "SELECT opened_at FROM fly WHERE player_id = ? ORDER BY id",
                        (current_player(),),
                    )
                ]
            finally:
                conn.close()
        pnl = frame.get("pnl")
        em_open = _num(frame.get("em_open"))
        first_em, first_pct = _reading_at(marks.tape, opened[0]) if opened else (None, None)
        _include_official(path, marks.ts, pnl)
        span = _range_text(*_pnl_extremes(path))
        yield DayResult(
            day,
            _report(
                day,
                spec.summary(),
                frame,
                settled=settled,
                tape=marks.tape or [],
                opened=opened,
                events=events,
                pnl_span=span,
            ),
            float(pnl) if isinstance(pnl, (int, float)) else None,
            em_open,
            first_em,
            first_pct,
            span,
        )


def _print_pnl(text: str, days: list[date], *, replay: Path | None) -> None:
    print(parse_spec(text).summary())
    total = 0
    counted = False
    for item in _walk(text, days, replay=replay):
        gap = f"  {item.pnl_span}  " if item.pnl_span else "  "
        print(
            f"{item.day.isoformat():<12}{_money(item.pnl)}{gap}"
            f"open EM {_quote(item.em_open):>6}  em {_quote(item.em_at_open):>6}  em% {_quote(item.em_pct):>5}",
            flush=True,
        )
        amount = _dollar_amount(item.pnl)
        if amount is not None:
            total += amount
            counted = True
    shown = f"{total:+12,.0f}" if counted else f"{'—':>12}"
    print(f"{'Total':<12}{shown}")


def _replay_dates(replay: Path | None) -> list[date]:
    """Baked session dates, oldest first. Filenames only, so the files stay unread."""
    if replay is not None:
        directories = [replay]
    else:
        from iron_fly.quotes.replay import _shipped_replay_dirs

        directories = _shipped_replay_dirs()
    found: set[date] = set()
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.glob("????-??-??.json"):
            try:
                found.add(date.fromisoformat(path.stem))
            except ValueError:
                continue
    return sorted(found)


def _days(
    text: str,
    requested: list[str],
    last: int | None,
    replay: Path | None,
    *,
    skip: str = "",
) -> list[date]:
    if last is not None:
        if last < 1:
            raise ValueError("--last must be at least 1")
        available = _replay_dates(replay)
        if not available:
            where = replay if replay is not None else Path("replay")
            raise ValueError(f"no replay files in {where}")
        chosen = available[-last:]
    elif requested:
        chosen = [date.fromisoformat(item) for item in requested]
    else:
        raw = yaml.safe_load(text or "")
        found = raw.get("date") if isinstance(raw, dict) else None
        if found in (None, ""):
            names = ", ".join(baked_days()) or "none"
            raise ValueError(f"pass --day YYYY-MM-DD, --last N, or set date in the config. Baked days: {names}")
        chosen = [date.fromisoformat(str(found))]
    return _without_skipped(chosen, skip)


def _without_skipped(days: list[date], skip: str) -> list[date]:
    skipped = _skip_dates(skip)
    if not skipped:
        return days
    kept = [day for day in days if day not in skipped]
    if not kept:
        listed = ", ".join(day.isoformat() for day in days)
        raise ValueError(f"--skip leaves no sessions (selected {listed})")
    return kept


def _skip_dates(raw: str) -> set[date]:
    found: set[date] = set()
    for item in raw.split(","):
        text = item.strip()
        if not text:
            continue
        try:
            found.add(date.fromisoformat(text))
        except ValueError:
            raise ValueError(f"--skip dates must be YYYY-MM-DD, got {text}") from None
    return found


def _wings_label(fly: dict) -> str:
    put = fly.get("wings")
    call = fly.get("call_wings")
    if call in (None, "", put):
        return str(put)
    return f"{put}/{call}"


def _settings(data_dir: Path) -> Settings:
    return Settings.from_values(
        app_base_url="http://localhost:8001",
        session_secret="cli",
        secrets_key=Fernet.generate_key().decode(),
        data_dir=data_dir,
        desk_mode="backtest-public",
    )


def _report(
    day: date,
    summary: str,
    frame: dict,
    *,
    settled: bool,
    tape: list | None = None,
    opened: list[datetime] | None = None,
    events: list | None = None,
    pnl_span: str = "",
) -> str:
    lines = [f"{day.isoformat()}  {summary} · open EM {_quote(frame.get('em_open'))}"]
    flies = frame.get("flies") or []
    if not flies:
        lines.append("  no flies")
    stamps = [int(point["time"]) for point in tape or []]
    opens = opened or []
    for index, fly in enumerate(flies):
        close = fly.get("time_close") or ("16:00:00" if settled and fly.get("status") == "active" else "open")
        strike = int(fly["strike"]) if fly.get("strike") is not None else "—"
        sto = "—" if fly.get("sto") is None else f"{float(fly['sto']):.2f}"
        when = opens[index] if index < len(opens) else None
        em, pct = _reading_at(tape or [], when, stamps)
        lines.append(
            f"  {fly.get('time_open') or '—':<8} {close:<8}  {strike}  wings {_wings_label(fly)}  "
            f"sto {sto}  em {_quote(em):>6}  em% {_quote(pct):>5}  pnl {_dollars(fly.get('pnl'))}"
        )
    if frame.get("parked_cash_points") or frame.get("parked_value_points"):
        lines.append(
            f"Parked wings cash {_dollars(frame.get('parked_cash_points'))} "
            f"value {_dollars(frame.get('parked_value_points'))}"
        )
    pnl_line = f"P&L {_dollars(frame.get('pnl'))}"
    if pnl_span:
        pnl_line += f"  {pnl_span}"
    lines.append(pnl_line)
    lines.append("")
    lines.append("Events")
    lines.append("----------")
    if not events:
        lines.append("  none")
    else:
        for event in events:
            lines.append(f"  {_event_clock(event.get('at'))} {event.get('text')}")
    return "\n".join(lines)


def _reading_at(
    tape: list,
    when: datetime | None,
    stamps: list[int] | None = None,
) -> tuple[float | None, float | None]:
    """ATM straddle and EM% on the latest tape print at or before `when`."""
    if when is None or not tape:
        return None, None
    stamps = stamps if stamps is not None else [int(point["time"]) for point in tape]
    index = bisect_right(stamps, int(when.timestamp())) - 1
    if index < 0:
        return None, None
    point = tape[index]
    return _num(point.get("em")), _num(point.get("pct"))


def _include_official(path: list[tuple[datetime, float]], when: datetime | None, pnl: object) -> None:
    """Keep the printed close inside the session range when the last mark differs."""
    if when is None or not isinstance(pnl, (int, float)) or isinstance(pnl, bool):
        return
    value = float(pnl)
    if path and abs(path[-1][1] - value) < 5e-4:
        return
    path.append((when, value))


def _pnl_extremes(
    path: list[tuple[datetime, float]],
) -> tuple[float | None, datetime | None, float | None, datetime | None]:
    """First time the session P&L printed its low, and the first time it printed its high."""
    if not path:
        return None, None, None, None
    lo = hi = path[0]
    for item in path[1:]:
        if item[1] < lo[1]:
            lo = item
        if item[1] > hi[1]:
            hi = item
    return lo[1], lo[0], hi[1], hi[0]


def _range_text(
    lo: float | None,
    lo_at: datetime | None,
    hi: float | None,
    hi_at: datetime | None,
) -> str:
    if lo is None or hi is None or lo_at is None or hi_at is None:
        return ""
    return f"min {_dollars(lo)} at {_clock(lo_at)}  max {_dollars(hi)} at {_clock(hi_at)}"


def _clock(when: datetime) -> str:
    if when.tzinfo is not None:
        when = when.astimezone(_ET)
    return when.strftime("%H:%M:%S")


def _event_clock(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        return "—"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_ET)
    return parsed.strftime("%H:%M:%S")


def _quote(value: object) -> str:
    number = _num(value)
    if number is None:
        return "—"
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _dollars(points: object) -> str:
    amount = _dollar_amount(points)
    if amount is None:
        return "—"
    return f"{amount:+,.0f}"


def _money(points: object) -> str:
    amount = _dollar_amount(points)
    if amount is None:
        return f"{'—':>12}"
    return f"{amount:+12,.0f}"


def _dollar_amount(points: object) -> int | None:
    if not isinstance(points, (int, float)):
        return None
    return round(float(points) * 100)


if __name__ == "__main__":
    raise SystemExit(main())
