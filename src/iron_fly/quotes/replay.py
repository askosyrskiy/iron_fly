"""Baked replay day tapes and chain books. No live quote sources."""

from __future__ import annotations

import array
import gc
import json
import logging
import os
import threading
from bisect import bisect_right
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from iron_fly.domain.market import ChainSnapshot, DeskMarks, StrikeQuote, downsample, mid, put40_width, straddle_mid

log = logging.getLogger("iron_fly.quotes.replay")

_ET = ZoneInfo("America/New_York")
_CACHE_VERSION = 4
_SECOND_KEY = 1_000_000_000
_DAY_CACHE: dict[tuple[str, str], DeskMarks] = {}
_CHAIN_MEMORY: dict[str, dict[int, ChainSnapshot]] = {}
_PARSED_FILES: dict[tuple[str, int], tuple[DeskMarks, dict[int, ChainSnapshot]]] = {}
_PARSE_LOCK = threading.Lock()
_BAKED_NAMES: list[str] | None = None


def _ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_ET)
    return parsed


def _pack_float(value: float | None) -> float:
    if value is None:
        return float("nan")
    return float(value)


def _unpack_float(value: float) -> float | None:
    if value != value:
        return None
    return float(value)


def _num(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


class _ChainSeries:
    """Chain books keyed by the unix second of each tape print.

    Quotes are packed into one array so a session stays small enough for the
    Lightsail micro container. The file still stores the first book in full and
    later prints as the strikes that changed. Checkpoints keep a lookup from
    replaying the whole day. Missing quotes are NaN in the array.
    """

    def __init__(self, stamps: list[int], updates: list[tuple[tuple, ...]]) -> None:
        self.stamps = stamps
        self._counts = array.array("I")
        self._offsets = array.array("I")
        self._data = array.array("d")
        offset = 0
        for rows in updates:
            self._offsets.append(offset)
            self._counts.append(len(rows))
            for row in rows:
                self._data.append(float(row[0]))
                self._data.append(_pack_float(row[1]))
                self._data.append(_pack_float(row[2]))
                self._data.append(_pack_float(row[3]))
                self._data.append(_pack_float(row[4]))
            offset += len(rows) * 5
        self._checkpoints: dict[int, dict[float, tuple]] = {}
        self._lock = threading.Lock()
        self._cached: ChainSnapshot | None = None
        self._cached_lo = -1
        self._cached_hi = -1
        book: dict[float, tuple] = {}
        for index in range(len(stamps)):
            self._apply(book, index)
            if index % 30 == 0:
                self._checkpoints[index] = dict(book)

    def _apply(self, book: dict[float, tuple], index: int) -> None:
        begin = self._offsets[index]
        for step in range(self._counts[index]):
            cursor = begin + step * 5
            book[self._data[cursor]] = (
                self._data[cursor + 1],
                self._data[cursor + 2],
                self._data[cursor + 3],
                self._data[cursor + 4],
            )

    def asof(self, when: datetime) -> ChainSnapshot | None:
        stamp = int(when.timestamp())
        index = bisect_right(self.stamps, stamp) - 1
        if index < 0:
            return None
        start = (index // 30) * 30
        lo = self.stamps[index]
        hi = self.stamps[index + 1] - 1 if index + 1 < len(self.stamps) else 2**62
        with self._lock:
            if self._cached is not None and self._cached_lo == lo and self._cached_hi == hi:
                return self._cached
            book = dict(self._checkpoints[start])
            for cursor in range(start + 1, index + 1):
                self._apply(book, cursor)
            quotes = tuple(
                StrikeQuote(
                    strike=strike,
                    call_bid=_unpack_float(quote[0]),
                    call_ask=_unpack_float(quote[1]),
                    put_bid=_unpack_float(quote[2]),
                    put_ask=_unpack_float(quote[3]),
                )
                for strike, quote in sorted(book.items())
            )
            snap = ChainSnapshot(ts=None, expiration=None, strikes=quotes)
            self._cached = snap
            self._cached_lo = lo
            self._cached_hi = hi
            return snap

    def export(self) -> dict[str, list]:
        out: dict[str, list] = {}
        for index, stamp in enumerate(self.stamps):
            begin = self._offsets[index]
            rows: list[list] = []
            for step in range(self._counts[index]):
                cursor = begin + step * 5
                rows.append(
                    [
                        self._data[cursor],
                        _unpack_float(self._data[cursor + 1]),
                        _unpack_float(self._data[cursor + 2]),
                        _unpack_float(self._data[cursor + 3]),
                        _unpack_float(self._data[cursor + 4]),
                    ]
                )
            out[str(stamp)] = rows
        return out


def chain_memory(day: date, when: datetime) -> ChainSnapshot | None:
    """Chain current at `when`. A 2-second series uses the latest print at or before it.

    An older file keyed by minute still returns that minute's book.
    """
    book = _CHAIN_MEMORY.get(day.isoformat())
    if isinstance(book, _ChainSeries):
        return book.asof(when)
    if not book:
        return None
    return book.get(int(when.timestamp()) // 60)


def has_chain_memory(day: date) -> bool:
    return bool(_CHAIN_MEMORY.get(day.isoformat()))


class ChainCursor:
    """Forward scan of one session's chain. Advance with nondecreasing stamps."""

    def __init__(self, day: date) -> None:
        book = _CHAIN_MEMORY.get(day.isoformat())
        self._series = book if isinstance(book, _ChainSeries) else None
        self._minutes = book if isinstance(book, dict) else None
        self._index = -1
        self._quotes: dict[float, tuple] = {}
        self._minute: int | None = None
        self._by_strike: dict[float, StrikeQuote] = {}

    def advance(self, stamp: int) -> bool:
        """Apply every stored print at or before `stamp`. True when the book moved."""
        if self._series is not None:
            changed = False
            stamps = self._series.stamps
            while self._index + 1 < len(stamps) and stamps[self._index + 1] <= stamp:
                self._index += 1
                self._series._apply(self._quotes, self._index)
                changed = True
            return changed
        if not self._minutes:
            return False
        minute = stamp // 60
        if minute == self._minute:
            return False
        snap = self._minutes.get(minute)
        if snap is None:
            return False
        self._minute = minute
        self._by_strike = {row.strike: row for row in snap.strikes}
        return True

    def mid(self, strike: float, right: str) -> float | None:
        if self._series is not None:
            packed = self._quotes.get(float(strike))
            if packed is None:
                return None
            if right == "call":
                bid, ask = _unpack_float(packed[0]), _unpack_float(packed[1])
            else:
                bid, ask = _unpack_float(packed[2]), _unpack_float(packed[3])
            return mid(bid, ask)
        row = self._by_strike.get(float(strike))
        if row is None:
            return None
        bid, ask = (row.call_bid, row.call_ask) if right == "call" else (row.put_bid, row.put_ask)
        return mid(bid, ask)

    def combo(self, strike: float, wings: int, call_wings: int | None = None) -> float | None:
        """Same 4-leg mid as combo_mark: shorts minus wings."""
        call = wings if call_wings is None else call_wings
        short_call = self.mid(strike, "call")
        short_put = self.mid(strike, "put")
        long_call = self.mid(strike + call, "call")
        long_put = self.mid(strike - wings, "put")
        if None in (short_call, short_put, long_call, long_put):
            return None
        return round(float(short_call) + float(short_put) - float(long_call) - float(long_put), 2)


def store_chain_rows(day: date, rows: object) -> int:
    """Index one book per stored chain print so later lookups stay in memory."""
    built = _series_from_rows(rows)
    if built is None:
        return 0
    _CHAIN_MEMORY[day.isoformat()] = built
    return len(built.stamps)


def apply_chain_records(day: date, marks: DeskMarks, records) -> int:
    """Paint EM and store chain deltas from rows ordered by unix second.

    Each record is ``(stamp, strike, call_bid, call_ask, put_bid, put_ask)``.
    A stamp's strikes are one stored snapshot. Unchanged strikes are omitted
    after the first snapshot.
    """
    latest: dict[float, tuple] = {}
    stamps: list[int] = []
    updates: list[tuple[tuple, ...]] = []
    active: dict[float, StrikeQuote] = {}
    active_ts: int | None = None
    index = 0
    tape = marks.tape or []

    def publish(until: int) -> None:
        nonlocal index
        while index < len(tape) and int(tape[index]["time"]) < until:
            if active_ts is not None and int(tape[index]["time"]) >= active_ts and active:
                _fill_em(tape[index], active, marks.em_open)
            index += 1

    def commit(stamp: int, book: dict[float, StrikeQuote]) -> None:
        changed: list[tuple] = []
        for strike, quote in book.items():
            packed = (quote.call_bid, quote.call_ask, quote.put_bid, quote.put_ask)
            if latest.get(strike) == packed:
                continue
            latest[strike] = packed
            changed.append((strike, *packed))
        if not changed:
            return
        if not stamps:
            changed = [(strike, *latest[strike]) for strike in sorted(latest)]
        stamps.append(stamp)
        updates.append(tuple(changed))

    for stamp, strike, call_bid, call_ask, put_bid, put_ask in records:
        stamp = int(stamp)
        strike = float(strike)
        if active_ts is None:
            active_ts = stamp
        if stamp != active_ts:
            publish(stamp)
            commit(active_ts, active)
            active = {}
            active_ts = stamp
        active[strike] = StrikeQuote(
            strike=strike,
            call_bid=_num(call_bid),
            call_ask=_num(call_ask),
            put_bid=_num(put_bid),
            put_ask=_num(put_ask),
        )
    if active_ts is not None:
        publish(10**18)
        commit(active_ts, active)
    if not stamps:
        return 0
    _CHAIN_MEMORY[day.isoformat()] = _ChainSeries(stamps, updates)
    _seed_em(marks)
    return len(stamps)


def _series_from_rows(rows: object) -> _ChainSeries | None:
    if not isinstance(rows, list):
        return None
    grouped: dict[int, list[dict]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        when = _ts(item.get("ts"))
        if when is None or _num(item.get("strike")) is None:
            continue
        grouped.setdefault(int(when.timestamp()), []).append(item)
    latest: dict[float, tuple] = {}
    stamps: list[int] = []
    updates: list[tuple[tuple, ...]] = []
    for stamp in sorted(grouped):
        changed: list[tuple] = []
        for item in grouped[stamp]:
            strike = float(item["strike"])
            quote = (
                _num(item.get("call_bid")),
                _num(item.get("call_ask")),
                _num(item.get("put_bid")),
                _num(item.get("put_ask")),
            )
            if latest.get(strike) == quote:
                continue
            latest[strike] = quote
            changed.append((strike, *quote))
        if not changed:
            continue
        if not stamps:
            changed = [(strike, *latest[strike]) for strike in sorted(latest)]
        stamps.append(stamp)
        updates.append(tuple(changed))
    if not stamps:
        return None
    return _ChainSeries(stamps, updates)


def write_day_cache(root: Path, day: date, marks: DeskMarks) -> None:
    """Write one session file. See `_cache_payload` for the version 4 layout."""
    _write_day_cache(root, day, marks)


def note_baked_days(names: list[str]) -> None:
    """Remember the sessions already parsed for every player in this process."""
    global _BAKED_NAMES
    _BAKED_NAMES = list(names)


def baked_days() -> list[str]:
    """Session dates shipped in the replay cache. Shipped replay cache only."""
    if _BAKED_NAMES is not None:
        return list(_BAKED_NAMES)
    found: set[str] = set()
    for directory in _shipped_replay_dirs():
        for path in directory.glob("????-??-??.json"):
            if _read_payload(path) is not None:
                found.add(path.stem)
    return sorted(found)


def release_cached_day(day: str) -> None:
    """Drop one session so a small container can open another day."""
    _CHAIN_MEMORY.pop(day, None)
    for key in [item for item in _PARSED_FILES if Path(item[0]).stem == day]:
        del _PARSED_FILES[key]
    for key in [item for item in _DAY_CACHE if item[1] == day]:
        del _DAY_CACHE[key]


def read_cached_day(root: Path, day: date) -> DeskMarks | None:
    """A day file from `root` or the replay shipped with the app.

    The first read parses the file and keeps the tape and chain book in memory
    for every player in this process. Later reads reuse that parse. Each caller
    gets its own marks header so a playhead seek does not move the shared copy.
    """
    for directory in [root, *_shipped_replay_dirs()]:
        path = directory / f"{day.isoformat()}.json"
        if not path.is_file():
            continue
        try:
            stamp = path.stat().st_mtime_ns
            resolved = str(path.resolve())
        except OSError:
            continue
        key = (resolved, stamp)
        with _PARSE_LOCK:
            hit = _PARSED_FILES.get(key)
            if hit is not None:
                marks, chains = hit
                _CHAIN_MEMORY.setdefault(day.isoformat(), chains)
                return replace(marks, chain=None)
            payload = _read_payload(path)
            if payload is None:
                continue
            if payload.get("version") not in (3, _CACHE_VERSION) or not _chain_table(payload.get("chains")):
                continue
            marks = _marks_from_payload(day, payload)
            del payload
            gc.collect()
            chains = _CHAIN_MEMORY.get(day.isoformat(), {})
            for old in [item for item in _PARSED_FILES if item[0] == resolved and item[1] != stamp]:
                del _PARSED_FILES[old]
            _PARSED_FILES[key] = (marks, chains)
            return replace(marks, chain=None)


def _baked_replay_dirs() -> list[Path]:
    """Directories shipped with the app. The writable data dir is searched first."""
    found: list[Path] = []
    named = os.environ.get("REPLAY_CACHE", "").strip()
    candidates = [Path(named)] if named else []
    candidates.append(Path("/app/replay"))
    here = Path(__file__).resolve()
    candidates.append(here.parents[2] / "replay")
    for path in candidates:
        if path.is_dir() and path not in found:
            found.append(path)
    return found


def _shipped_replay_dirs() -> list[Path]:
    """Replay files copied into the image, plus the repo `replay/` folder in a checkout."""
    found = _baked_replay_dirs()
    checkout = Path(__file__).resolve().parents[3] / "replay"
    if checkout.is_dir() and checkout not in found:
        found.append(checkout)
    return found


def _read_day_cache(root: Path, day: date) -> DeskMarks | None:
    """Newest complete file wins. A version 2 tape is used only when nothing newer exists."""
    partial: dict | None = None
    for directory in [root, *_baked_replay_dirs()]:
        payload = _read_payload(directory / f"{day.isoformat()}.json")
        if payload is None:
            continue
        version = payload.get("version")
        if version == _CACHE_VERSION and _chain_table(payload.get("chains")):
            return _marks_from_payload(day, payload)
        if partial is None and version in (2, 3):
            partial = payload
    if partial is None:
        return None
    return _marks_from_payload(day, partial)


def _read_payload(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    tape = payload.get("tape")
    if not isinstance(tape, list) or not tape:
        return None
    return payload


def _marks_from_payload(day: date, payload: dict) -> DeskMarks:
    tape = payload["tape"]
    marks = DeskMarks(source="replay", tape=tape)
    marks.last = _num(payload.get("last"))
    marks.em_open = _num(payload.get("em_open"))
    marks.em_now = _num(payload.get("em_now"))
    marks.em_pct = _num(payload.get("em_pct"))
    marks.put_40_width = _num(payload.get("wings"))
    stamp = tape[0].get("time") if isinstance(tape[0], dict) else None
    if isinstance(stamp, (int, float)):
        marks.ts = datetime.fromtimestamp(stamp, tz=_ET)
    _load_chain_payload(day, payload.get("chains"))
    return marks


def _write_day_cache(root: Path, day: date, marks: DeskMarks) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{day.isoformat()}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_cache_payload(day, marks), separators=(",", ":")))
        tmp.replace(path)
    except OSError:
        return


def _cache_payload(day: date, marks: DeskMarks) -> dict:
    """Version 4 day file: `replay/YYYY-MM-DD.json`.

    ``tape`` is every stored SPX second (unix ``time``, ``value``, and painted
    ``em``, ``pct``, ``wings``). ``chains`` maps the unix second of each chain
    print to the strikes that changed. The first second is the full book. Each
    strike is ``[strike, call_bid, call_ask, put_bid, put_ask]``, with nulls
    allowed. A lookup uses the latest second at or before the playhead.
    A version 3 file still loads: its keys are unix minutes.
    A file is complete when ``version`` is 4 and ``chains`` is not empty.
    """
    return {
        "version": _CACHE_VERSION,
        "day": day.isoformat(),
        "last": marks.last,
        "em_open": marks.em_open,
        "em_now": marks.em_now,
        "em_pct": marks.em_pct,
        "wings": marks.put_40_width,
        "tape": marks.tape,
        "chains": _export_chains(day),
    }


def _export_chains(day: date) -> dict[str, list]:
    book = _CHAIN_MEMORY.get(day.isoformat())
    if isinstance(book, _ChainSeries):
        return book.export()
    if not book:
        return {}
    out: dict[str, list] = {}
    previous: list | None = None
    for minute in sorted(book):
        encoded = [
            [quote.strike, quote.call_bid, quote.call_ask, quote.put_bid, quote.put_ask]
            for quote in book[minute].strikes
        ]
        if encoded == previous:
            continue
        out[str(minute)] = encoded
        previous = encoded
    return out


def _chain_table(raw: object) -> dict:
    return raw if isinstance(raw, dict) and raw else {}


def _load_chain_payload(day: date, raw: object) -> None:
    table = _chain_table(raw)
    if not table:
        return
    if _second_keyed(table):
        series = _series_from_table(table)
        if series is not None:
            _CHAIN_MEMORY[day.isoformat()] = series
        return
    parsed: dict[int, ChainSnapshot] = {}
    for key, rows in table.items():
        try:
            minute = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(rows, list):
            continue
        quotes: list[StrikeQuote] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 5 or _num(row[0]) is None:
                continue
            quotes.append(
                StrikeQuote(
                    strike=float(row[0]),
                    call_bid=_num(row[1]),
                    call_ask=_num(row[2]),
                    put_bid=_num(row[3]),
                    put_ask=_num(row[4]),
                )
            )
        if quotes:
            quotes.sort(key=lambda quote: quote.strike)
            parsed[minute] = ChainSnapshot(ts=None, expiration=day, strikes=tuple(quotes))
    if not parsed:
        return
    start, end = min(parsed), max(parsed)
    carried = parsed[start]
    filled: dict[int, ChainSnapshot] = {}
    for minute in range(start, end + 1):
        if minute in parsed:
            carried = parsed[minute]
        filled[minute] = carried
    _CHAIN_MEMORY[day.isoformat()] = filled


def _second_keyed(table: dict) -> bool:
    for key in table:
        try:
            if int(key) >= _SECOND_KEY:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _series_from_table(table: dict) -> _ChainSeries | None:
    parsed: list[tuple[int, tuple]] = []
    for key, rows in table.items():
        try:
            stamp = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(rows, list):
            continue
        changed: list[tuple] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 5 or _num(row[0]) is None:
                continue
            changed.append((float(row[0]), _num(row[1]), _num(row[2]), _num(row[3]), _num(row[4])))
        if changed:
            parsed.append((stamp, tuple(changed)))
    if not parsed:
        return None
    parsed.sort(key=lambda item: item[0])
    return _ChainSeries([item[0] for item in parsed], [item[1] for item in parsed])


def _round_to_5(spot: float) -> float:
    return float(5 * round(spot / 5))


def _paint_em(tape: list[dict[str, float]], rows: object, em_open: float | None) -> None:
    """Carry each chain window forward onto the SPX tape and store EM and wings."""
    if not isinstance(rows, list) or not tape:
        return
    active: dict[float, StrikeQuote] = {}
    active_ts: int | None = None
    index = 0

    def publish(until: int) -> None:
        nonlocal index
        while index < len(tape) and int(tape[index]["time"]) < until:
            if active_ts is not None and int(tape[index]["time"]) >= active_ts and active:
                _fill_em(tape[index], active, em_open)
            index += 1

    for item in rows:
        if not isinstance(item, dict):
            continue
        when = _ts(item.get("ts"))
        strike = _num(item.get("strike"))
        if when is None or strike is None:
            continue
        stamp = int(when.timestamp())
        if active_ts is None:
            active_ts = stamp
        if stamp != active_ts:
            publish(stamp)
            active = {}
            active_ts = stamp
        active[strike] = StrikeQuote(
            strike=strike,
            call_bid=_num(item.get("call_bid")),
            call_ask=_num(item.get("call_ask")),
            put_bid=_num(item.get("put_bid")),
            put_ask=_num(item.get("put_ask")),
        )
    publish(10**18)


def _fill_em(point: dict[str, float], quotes: dict[float, StrikeQuote], em_open: float | None) -> None:
    chain = ChainSnapshot(ts=None, expiration=None, strikes=tuple(quotes.values()))
    atm = _round_to_5(float(point["value"]))
    em = straddle_mid(chain, atm)
    width = put40_width(chain, atm)
    if em is not None:
        point["em"] = em
    if width is not None:
        point["wings"] = width
    if em is not None and em_open is not None and em_open > 0:
        point["pct"] = round(100 * em / em_open, 1)


def _seed_em(marks: DeskMarks) -> None:
    if not marks.tape:
        return
    first = marks.tape[0]
    if "em" in first:
        marks.em_now = first["em"]
    if "wings" in first:
        marks.put_40_width = first["wings"]
    if "pct" in first:
        marks.em_pct = first["pct"]


def _tape(samples: object, limit: int | None = 480) -> list[dict[str, float]]:
    by_time: dict[int, float] = {}
    if isinstance(samples, list):
        for item in samples:
            if not isinstance(item, dict):
                continue
            last = _num(item.get("last"))
            when = _ts(item.get("ts"))
            if last is None or when is None:
                continue
            by_time[int(when.timestamp())] = last
    points = [{"time": stamp, "value": by_time[stamp]} for stamp in sorted(by_time)]
    if limit is None:
        return points
    return downsample(points, limit)


def chains_keep_each_print(chains: object) -> bool:
    """True when chain keys are unix seconds and average about one print per few seconds."""
    if not isinstance(chains, dict) or len(chains) < 2:
        return False
    keys: list[int] = []
    for key in chains:
        try:
            keys.append(int(key))
        except (TypeError, ValueError):
            return False
    keys.sort()
    if keys[0] < _SECOND_KEY:
        return False
    span = keys[-1] - keys[0]
    return span > 0 and len(keys) * 6 >= span


def tape_keeps_each_second(tape: object) -> bool:
    """True when the series is about one print a second across its span."""
    if not isinstance(tape, list) or len(tape) < 2:
        return False
    first = tape[0].get("time") if isinstance(tape[0], dict) else None
    last = tape[-1].get("time") if isinstance(tape[-1], dict) else None
    if not isinstance(first, (int, float)) or not isinstance(last, (int, float)):
        return False
    span = int(last) - int(first)
    return span > 0 and len(tape) * 2 >= span
