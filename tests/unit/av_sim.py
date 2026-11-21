"""Drive the real Alpha Vantage ledger hour by hour against a fake store, clock and server."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta

from tests.unit.fakes import FakeClock, FakeStore
from weather_signal.ingest import av_ledger as av
from weather_signal.sessions import Calendar

ROOT = "/Volumes/c/raw/ledger"


def served(cal: Calendar, now: datetime, lag_h: float) -> date:
    """The newest bar the server has at `now`: closes plus the serving lag."""
    return cal.latest_closed_before(now, margin=timedelta(hours=lag_h))


def simulate(
    cal: Calendar,
    start: datetime,
    hours: int,
    lag_h: float = 0.0,
    *,
    offset_s: Callable[[datetime], float] = lambda t: 330.0,
    fail: frozenset[datetime] = frozenset(),
    store: FakeStore | None = None,
    before_slot: Callable[[datetime, FakeStore], None] | None = None,
) -> tuple[list[tuple[datetime, av.SlotReport]], FakeStore]:
    store = store or FakeStore()
    out = []
    for i in range(hours):
        slot = start + timedelta(hours=i)
        if before_slot:
            before_slot(slot, store)
        clock = FakeClock(slot + timedelta(seconds=offset_s(slot)))
        ledger = av.Ledger(ROOT, store)

        def call(ticker: str, slot: datetime = slot, clock: FakeClock = clock) -> av.CallResult:
            if slot in fail:
                return av.CallResult("rate_note")
            return av.CallResult("landed", served(cal, clock.now(), lag_h))

        out.append((slot, av.run_slot(ledger, cal, av.Clock(clock.now, clock.sleep), call)))
    return out, store


def counts(reports: list[tuple[datetime, av.SlotReport]], day: date) -> dict[str, int]:
    c = {"WMT": 0, "SPY": 0, "XLP": 0}
    for slot, r in reports:
        if slot.date() == day and r.sent_at is not None and r.ticker:
            c[r.ticker] += 1
    return c


def sends(reports: list[tuple[datetime, av.SlotReport]]) -> list[datetime]:
    return sorted(r.sent_at for _, r in reports if r.sent_at is not None)
