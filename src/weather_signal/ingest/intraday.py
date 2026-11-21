"""Hourly WMT bars for display, from Yahoo's chart endpoint (60m, range 5d); nothing models on them.

Fetched at :05 only inside an XNYS session of the seed calendar, up to an hour past its close, and
landed once per hour as an envelope under source `yahoo-intraday`. The pipeline's
`daily.prices_intraday` reads those envelopes alone; `prices_raw` and every model input never do.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from weather_signal import config
from weather_signal.ingest import hourly, sources
from weather_signal.ingest.fetch import FetchFailed, RetryPolicy, Transport, fetch
from weather_signal.ingest.files import Store
from weather_signal.sessions import Calendar

log = logging.getLogger(__name__)
# What a malformed body raises while parsed: bad JSON or shape, or a number out of range.
UNREADABLE = (KeyError, IndexError, TypeError, ValueError, ArithmeticError)
SOURCE = "yahoo-intraday"
TABLE_SOURCE = "yahoo_chart_60m"
TICKER = config.PRIMARY
PARAMS = {"interval": "60m", "range": "5d"}
RETAIN_SESSIONS = 5
BAR = timedelta(hours=1)
AFTER_CLOSE = timedelta(hours=1)  # one run past the close lands the final bar
POLICY = RetryPolicy(attempts=2, delays=(5.0,), timeout=(10.0, 30.0))

Result = Literal["closed", "fetch_failed", "invalid", "created", "exists", "unknown"]


@dataclass(frozen=True)
class IntradayBar:
    ticker: str
    bar_start: datetime
    open: float | None
    high: float | None
    low: float | None
    close: float
    volume: int | None
    final: bool


def in_window(now: datetime, cal: Calendar) -> bool:
    """Inside today's session, from its open to an hour past its close (early closes included)."""
    s = cal.by_day.get(now.astimezone(UTC).date())
    return s is not None and s.open <= now < s.close + AFTER_CLOSE


def keep_from(now: datetime, cal: Calendar) -> datetime:
    """The open of the oldest of the last RETAIN_SESSIONS sessions up to `now`'s day."""
    day = now.astimezone(UTC).date()
    d = day if cal.is_session(day) else cal.previous(day)
    for _ in range(RETAIN_SESSIONS - 1):
        d = cal.previous(d)
    return cal.by_day[d].open


def _num(v: Any) -> float | None:
    return None if v is None else round(float(v), 4)


def bars(doc: dict[str, Any], fetched_at: datetime, cal: Calendar) -> list[IntradayBar]:
    """Bars on a session's hour grid with a close; the trailing last-quote point is off the grid."""
    res = doc["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        start = datetime.fromtimestamp(ts, UTC)
        s = cal.by_day.get(start.date())
        if q["close"][i] is None or s is None:
            continue
        if not s.open <= start < s.close or (start - s.open) % BAR:
            continue
        vol = q["volume"][i]
        out.append(
            IntradayBar(
                TICKER,
                start,
                _num(q["open"][i]),
                _num(q["high"][i]),
                _num(q["low"][i]),
                float(q["close"][i]),
                None if vol is None else int(vol),
                min(start + BAR, s.close) <= fetched_at,
            )
        )
    return out


def retained(body_text: str, fetched_at: datetime, cal: Calendar) -> list[IntradayBar]:
    """The bars one envelope contributes: on the grid and inside the retention window."""
    keep = keep_from(fetched_at, cal)
    return [b for b in bars(json.loads(body_text), fetched_at, cal) if b.bar_start >= keep]


def udf_rows(body_text: str, fetched_s: int, cal: Calendar) -> list[tuple[Any, ...]]:
    """The pipeline UDF's rows, times as epoch seconds: a UDF sees TIMESTAMPs as naive local time."""
    at = datetime.fromtimestamp(fetched_s, UTC)
    keep = int(keep_from(at, cal).timestamp())
    try:
        got = retained(body_text, at, cal)
    except UNREADABLE as err:  # one bad file must not halt the pipeline update
        log.warning("intraday envelope unreadable (%s); no rows", type(err).__name__)
        return []
    return [
        (b.ticker, int(b.bar_start.timestamp()), b.open, b.high, b.low, b.close, b.volume, b.final)
        + (keep,)
        for b in got
    ]


def fetch_and_land(
    store: Store, n: config.Names, cal: Calendar, transport: Transport, now: datetime
) -> Result:
    """One call in the window; a failed or unreadable fetch logs and lands nothing."""
    if not in_window(now, cal):
        return "closed"
    url = sources.YAHOO.format(ticker=TICKER)
    try:
        resp = fetch(SOURCE, url, PARAMS, policy=POLICY, transport=transport)
    except FetchFailed as err:
        log.warning("intraday %s not fetched: %s", TICKER, err.reason)
        return "fetch_failed"
    try:
        got = retained(resp.body.decode("utf-8"), resp.fetched_at, cal)
    except UNREADABLE as err:
        log.warning("intraday %s not landed: unreadable body (%s)", TICKER, type(err).__name__)
        return "invalid"
    if not got:
        log.warning("intraday %s not landed: no bar on the session grid", TICKER)
        return "invalid"
    slot_time = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    put = hourly.land(store, n.raw, SOURCE, url, PARAMS, slot_time.isoformat(), slot_time, resp)
    if put == "unknown":
        log.warning("intraday %s fetched but not landed: the PUT was unknown", TICKER)
    return put
