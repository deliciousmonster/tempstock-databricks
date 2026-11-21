"""A ticker's full Yahoo history in one call, landed once under its span; catch-up's price fetch."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time
from typing import Literal

from weather_signal import config
from weather_signal.config import BENCHMARK_FIRST, HISTORY_START
from weather_signal.ingest import envelope, hourly, sources, validate
from weather_signal.ingest.fetch import FetchFailed, Transport
from weather_signal.ingest.files import Store
from weather_signal.sessions import Calendar

# `settling` is retried on the next run: the newest close is not in the source yet.
Fetched = Literal["valid", "invalid", "throttled", "settling"]


def start_for(ticker: str) -> date:
    """The ticker's first session or HISTORY_START, whichever is later; its fetch, check and slot."""
    return max(HISTORY_START, BENCHMARK_FIRST.get(ticker, HISTORY_START))


def attempt_for(
    files: Store, n: config.Names, transport: Transport, cal: Calendar, now: datetime
) -> Callable[[str], tuple[Fetched, str]]:
    """One Yahoo call: 2xx and a full history lands; a fetch failure or unknown landing is throttled."""
    src = sources.YahooChartDaily(transport)
    end = now.astimezone(UTC).date()

    def attempt(ticker: str) -> tuple[Fetched, str]:
        start = start_for(ticker)
        slot = f"{start.isoformat()}..{end.isoformat()}"
        slot_time = datetime.combine(start, time(0), tzinfo=UTC)
        try:
            resp = src.fetch(ticker, start, end)
        except FetchFailed as err:
            return "throttled", err.reason
        v = validate.history(resp.body, resp.fetched_at, cal, start)
        if not v.ok:
            return ("settling" if v.flag == validate.SETTLING else "invalid"), v.reason
        url = sources.YAHOO.format(ticker=ticker)
        params = src.params(start, end)
        put = hourly.land(files, n.raw, "yahoo", url, params, slot, slot_time, resp)
        if put == "unknown":
            return "throttled", "landing unknown"
        key = envelope.landing_key("yahoo", url, params, slot)
        return "valid", envelope.landing_path(n.raw, "yahoo", slot_time, key)

    return attempt
