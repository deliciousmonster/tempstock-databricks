"""Open-Meteo backfill chunks for catch-up: spans, their weighted cost, the paced driver, the fetch."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from weather_signal.ingest import envelope, sources, validate
from weather_signal.ingest.fetch import ONE_SHOT, FetchFailed, RawResponse, Transport
from weather_signal.ingest.om_budget import Ceilings, Ledger, weighted_cost

log = logging.getLogger(__name__)
SOURCES = ("open-meteo-era5", "open-meteo-previous-runs")


@dataclass(frozen=True)
class Chunk:
    source: str
    start: date
    end: date
    variables: int
    locations: int = 50

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def cost(self) -> float:
        return weighted_cost(self.locations, self.days, self.variables)

    @property
    def slot(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"


def chunks(
    source: str, start: date, end: date, span_days: int, variables: int, grid: int = 1
) -> Iterator[Chunk]:
    """Full spans, then a tail cut to whole `grid` days, so its key holds for `grid` days."""
    d = start
    while d <= end:
        e = d + timedelta(days=span_days - 1)
        if e > end:
            whole = ((end - d).days + 1) // grid
            if whole == 0:
                return
            e = d + timedelta(days=whole * grid - 1)
        yield Chunk(source, d, e, variables)
        d = e + timedelta(days=1)


@dataclass
class Driver:
    ledger: Ledger
    now: Callable[[], datetime]
    sleep: Callable[[float], None]
    caps: Ceilings = Ceilings()

    def run(
        self,
        work: list[Chunk],
        exists: Callable[[Chunk], bool],
        send: Callable[[Chunk], bool],
        limit: int | None = None,
    ) -> int:
        """Send every chunk not already landed, at most `limit`; returns the number sent."""
        sent = 0
        for c in work:
            if limit is not None and sent >= limit:
                break
            if exists(c):
                continue
            at = self.ledger.earliest_send(self.now(), c.cost, self.caps)
            wait = (at - self.now()).total_seconds()
            if wait > 0:
                self.sleep(wait)
            send(c)
            self.ledger.add(self.now(), c.cost)
            sent += 1
        return sent


def request(c: Chunk) -> tuple[str, dict[str, str]]:
    if c.source == "open-meteo-era5":
        return sources.ARCHIVE, sources.era5_params(sources.metros(), c.start, c.end)
    return sources.PREVIOUS_RUNS, sources.previous_runs_params(sources.metros(), c.start, c.end)


def _slot_time(c: Chunk) -> datetime:
    return datetime.combine(c.start, time(0), tzinfo=UTC)


def landing(root: str, c: Chunk) -> str:
    """The chunk's path: its source, its first day, and the key of its request and span."""
    endpoint, params = request(c)
    key = envelope.landing_key(c.source, endpoint, params, c.slot)
    return envelope.landing_path(root, c.source, _slot_time(c), key)


def check(c: Chunk, body: bytes) -> validate.Verdict:
    """Shaped to the chunk's span, not the steady 9 hourly or 14 daily days."""
    if c.source == "open-meteo-era5":
        return validate.guarded(lambda: validate.era5_chunk(body, start=c.start, end=c.end))
    return validate.guarded(lambda: validate.previous_runs_backfill(body, start=c.start, end=c.end))


@dataclass
class Report:
    invalid: int = 0
    failed: int = 0
    unavailable: str = ""  # the reason, when the body was well-formed and empty at source


def fetch_checked(
    c: Chunk, transport: Transport, rep: Report
) -> tuple[RawResponse, str | None] | None:
    """The chunk's response and its flag when it fetched and validated; None, counted, otherwise."""
    endpoint, params = request(c)
    try:
        resp = sources.open_meteo(endpoint, params, transport, policy=ONE_SHOT)
    except FetchFailed as err:
        log.warning("backfill %s %s not fetched: %s", c.source, c.slot, err.reason)
        rep.failed += 1
        return None
    v = check(c, resp.body)
    if not v.ok and v.flag == validate.UNAVAILABLE:
        log.warning("backfill %s %s unavailable at source: %s", c.source, c.slot, v.reason)
        rep.unavailable = v.reason
        return None
    if not v.ok:
        log.warning("backfill %s %s not landed: %s", c.source, c.slot, v.reason)
        rep.invalid += 1
        return None
    return resp, v.flag
