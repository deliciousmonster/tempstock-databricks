"""Validate a response before it lands. Pure: bytes in, a verdict out."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from itertools import pairwise
from typing import Any

from weather_signal import asof
from weather_signal.sessions import Calendar

N_LOCATIONS = 50
ERA5_DAYS = 14
ERA5_MAX_NULL_SUFFIX = 10
ERA5_VARS = ("temperature_2m_mean", "temperature_2m_max", "temperature_2m_min")
MIN_SESSIONS = 5
# A close-to-close move this large is a split or a bad bar; the pipeline fails on it too
SPLIT_GUARD = 0.4


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""
    flag: str | None = None


OK = Verdict(True)
SETTLING = "settling"
# Well-formed, but the source holds no value for a whole column of the span.
UNAVAILABLE = "unavailable_at_source"


def parse_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None


def _locations(doc: Any) -> list[dict[str, Any]]:
    if isinstance(doc, dict):
        return [doc]
    return doc if isinstance(doc, list) else []


def _hourly(body: bytes, hours: int, n: int) -> tuple[list[dict[str, Any]], Verdict]:
    """The locations of a Previous Runs body of the expected shape, every lead column present."""
    locs = _locations(parse_json(body))
    if not locs or any("hourly" not in x for x in locs):
        return [], Verdict(False, "not a Previous Runs body")
    if len(locs) != n:
        return [], Verdict(False, f"{len(locs)} locations, expected {n}")
    for loc in locs:
        h = loc["hourly"]
        if len(h["time"]) != hours:
            return [], Verdict(False, f"{len(h['time'])} hours, expected {hours}")
        for lead in asof.LEADS:
            if h.get(f"temperature_2m_previous_day{lead}") is None:
                return [], Verdict(False, f"lead {lead} missing")
    return locs, OK


def previous_runs(
    body: bytes, fetched_at: datetime, p_hours: int, *, hours: int = 216, n: int = N_LOCATIONS
) -> Verdict:
    locs, v = _hourly(body, hours, n)
    if not v.ok:
        return v
    for loc in locs:
        h = loc["hourly"]
        if any(v is None for v in h["temperature_2m"]):
            return Verdict(False, "null in temperature_2m")
        for lead in asof.LEADS:
            vals = h[f"temperature_2m_previous_day{lead}"]
            for t, v in zip(h["time"], vals, strict=True):
                vt = datetime.fromisoformat(t).replace(tzinfo=UTC)
                if v is None and asof.known_at_forecast(vt, lead, p_hours) <= fetched_at:
                    return Verdict(False, f"null at lead {lead} {t} although known")
    return OK


def _span(
    body: bytes, key: str, cols: tuple[str, ...], first: datetime, last: datetime, n: int
) -> tuple[list[Any], Verdict]:
    """Every location holds `key` with `time` from `first` to `last` and each column as long."""
    locs = _locations(parse_json(body))
    if len(locs) != n:
        return locs, Verdict(False, f"{len(locs)} locations, expected {n}")
    for loc in locs:
        block = loc[key]
        times = block["time"]
        if (datetime.fromisoformat(times[0]), datetime.fromisoformat(times[-1])) != (first, last):
            return locs, Verdict(False, f"time {times[0]}..{times[-1]}, expected {first}..{last}")
        for col in ("time", *cols):
            if not isinstance(block[col], list) or len(block[col]) != len(times):
                return locs, Verdict(False, f"{col} not as long as time")
    return locs, OK


PREVIOUS_RUNS_COLS = ("temperature_2m", *(f"temperature_2m_previous_day{n}" for n in asof.LEADS))


def previous_runs_backfill(body: bytes, *, start: date, end: date, n: int = N_LOCATIONS) -> Verdict:
    """A history chunk: a hole will not fill on refetch, so nulls land flagged; daily drops them."""
    last = datetime.combine(end, time(23))
    locs, v = _span(body, "hourly", PREVIOUS_RUNS_COLS, datetime.combine(start, time(0)), last, n)
    hours = (end - start).days * 24 + 24
    if v.ok and len(locs[0]["hourly"]["time"]) != hours:
        v = Verdict(False, f"{len(locs[0]['hourly']['time'])} hours, expected {hours}")
    if not v.ok:
        return v
    if any(all(x is None for x in loc["hourly"]["temperature_2m"]) for loc in locs):
        return Verdict(False, "a location with no temperature_2m at all")
    for col in PREVIOUS_RUNS_COLS[1:]:
        if all(x is None for loc in locs for x in loc["hourly"][col]):
            return Verdict(False, f"{col} null at every location", flag=UNAVAILABLE)
    cols = [loc["hourly"][c] for loc in locs for c in PREVIOUS_RUNS_COLS]
    return Verdict(True, flag="interior_null") if any(None in c for c in cols) else OK


def era5_chunk(body: bytes, *, start: date, end: date, n: int = N_LOCATIONS) -> Verdict:
    """A history chunk: dated `start`..`end`, each variable as long, then the steady null rule."""
    first, last = datetime.combine(start, time(0)), datetime.combine(end, time(0))
    _, v = _span(body, "daily", ERA5_VARS, first, last, n)
    return v if not v.ok else era5_steady(body, days=(end - start).days + 1, n=n)


def guarded(check: Callable[[], Verdict]) -> Verdict:
    """Any JSON gets a verdict: a body that makes a check raise is invalid, not a crash."""
    try:
        return check()
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as err:
        return Verdict(False, f"malformed body: {type(err).__name__}")


def _null_suffix(values: list[Any]) -> tuple[int, bool]:
    """Length of the trailing null run, and whether a null sits before it."""
    k = len(values)
    while k > 0 and values[k - 1] is None:
        k -= 1
    return len(values) - k, any(v is None for v in values[:k])


def era5_steady(body: bytes, *, days: int = ERA5_DAYS, n: int = N_LOCATIONS) -> Verdict:
    locs = _locations(parse_json(body))
    if not locs or any("daily" not in x for x in locs):
        return Verdict(False, "not an archive body")
    if len(locs) != n:
        return Verdict(False, f"{len(locs)} locations, expected {n}")
    interior = False
    for loc in locs:
        d = loc["daily"]
        if len(d["time"]) != days:
            return Verdict(False, f"{len(d['time'])} days, expected {days}")
        for var in ERA5_VARS:
            suffix, hole = _null_suffix(d[var])
            if suffix > ERA5_MAX_NULL_SUFFIX:
                return Verdict(False, f"{var} null for the last {suffix} days")
            interior = interior or hole
    return Verdict(True, flag="interior_null") if interior else OK


def price_dates(doc: Any) -> list[date] | None:
    """Session dates in an Alpha Vantage or Yahoo body, or None when it is neither."""
    if isinstance(doc, dict) and isinstance(doc.get("Time Series (Daily)"), dict):
        return sorted(date.fromisoformat(d) for d in doc["Time Series (Daily)"])
    try:
        ts = doc["chart"]["result"][0]["timestamp"]
    except (KeyError, IndexError, TypeError):
        return None
    return sorted(datetime.fromtimestamp(t, UTC).date() for t in ts)


def prices(body: bytes, fetched_at: datetime, cal: Calendar) -> Verdict:
    dates = price_dates(parse_json(body))
    if dates is None:
        return Verdict(False, "no Time Series (Daily) or chart.result: a rate note or another page")
    if len(dates) < MIN_SESSIONS:
        return Verdict(False, f"{len(dates)} sessions, expected at least {MIN_SESSIONS}")
    newest_closed = cal.latest_closed_before(fetched_at)
    floor = cal.previous(cal.previous(newest_closed))
    if dates[-1] < floor:
        return Verdict(
            False, f"newest bar {dates[-1]} more than two sessions behind {newest_closed}"
        )
    return OK


def history(body: bytes, fetched_at: datetime, cal: Calendar, start: date) -> Verdict:
    """A full history: a close for every session from the first on or after `start`, no split."""
    first = prices(body, fetched_at, cal)
    if not first.ok:
        return first
    bars = _closes(parse_json(body))
    shape = _shape(bars, cal, start)
    if not shape.ok:
        return shape
    dates = {d for d, c in bars if c is not None}
    last = min(bars[-1][0], cal.latest_closed_before(fetched_at))
    expected = cal.between(start, last)
    if min(dates) > expected[0]:
        return Verdict(False, f"history starts {min(dates)}, after the first session {expected[0]}")
    missing = [d for d in expected if d not in dates]
    if missing == [cal.latest_closed_before(fetched_at)]:
        # Only the newest close is null: the source has not settled it yet; retry, never land.
        return Verdict(False, f"newest session {missing[0]} not settled", flag=SETTLING)
    if missing:
        return Verdict(False, f"{len(missing)} sessions missing or null, first {missing[0]}")
    return _no_split(bars)


def _closes(doc: Any) -> list[tuple[date, float | None]]:
    """A Yahoo body's (date, close) per timestamp, oldest first; empty when it has none."""
    try:
        res = doc["chart"]["result"][0]
        pairs = zip(res["timestamp"], res["indicators"]["quote"][0]["close"], strict=True)
        bars = sorted(pairs, key=lambda b: b[0])
    except (KeyError, IndexError, TypeError, ValueError):
        return []
    return [(datetime.fromtimestamp(ts, UTC).date(), c) for ts, c in bars]


def _shape(bars: list[tuple[date, float | None]], cal: Calendar, start: date) -> Verdict:
    """Every bar a session on or after `start`, and at least one close."""
    if not any(c is not None for _, c in bars):
        return Verdict(False, "no chart closes: every close null or none present")
    if bars[0][0] < start:
        return Verdict(False, f"bar on {bars[0][0]}, before the requested start {start}")
    off = [d for d, _ in bars if not cal.is_session(d)]
    if off:
        return Verdict(False, f"bar on {off[0]}, not a session")
    return OK


def _no_split(bars: list[tuple[date, float | None]]) -> Verdict:
    """Refuses a close moving SPLIT_GUARD or more from the one before it."""
    closes = [(d, c) for d, c in bars if c is not None]
    for (_, a), (day, b) in pairwise(closes):
        if a <= 0 or abs(b / a - 1) >= SPLIT_GUARD:
            return Verdict(False, f"close moved {a} to {b} on {day}, at or past {SPLIT_GUARD:.0%}")
    return OK
