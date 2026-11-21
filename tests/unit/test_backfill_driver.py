"""Backfill driver: minute, hour and day ceilings on a fake clock; resume by landed keys."""

from datetime import date, timedelta

from tests.conftest import utc
from tests.unit.fakes import FakeClock
from weather_signal.ingest.backfill import Chunk, Driver
from weather_signal.ingest.om_budget import Ceilings, Ledger


def _driver(t):
    clock = FakeClock(t)
    return Driver(Ledger(), clock.now, clock.sleep), clock


def _work(n, days=365, vars_=3):
    return [
        Chunk(
            "open-meteo-era5",
            date(1991, 1, 1) + timedelta(days=i * days),
            date(1991, 1, 1) + timedelta(days=i * days + days - 1),
            vars_,
        )
        for i in range(n)
    ]


def _sent_times(drv, clock, work):
    times = []
    drv.run(work, lambda c: False, lambda c: times.append(clock.now()) or True)
    return times


def test_no_window_exceeds_any_ceiling():
    drv, clock = _driver(utc(2026, 11, 18, 0))
    work = _work(400)
    times = _sent_times(drv, clock, work)
    cost = work[0].cost
    caps = Ceilings()
    for t in times:
        assert sum(cost for u in times if t - timedelta(minutes=1) < u <= t) <= caps.minute
        assert sum(cost for u in times if t - timedelta(hours=1) < u <= t) <= caps.hour
    by_day = {}
    for t in times:
        by_day[t.date()] = by_day.get(t.date(), 0) + cost
    assert max(by_day.values()) <= caps.day
    assert len(by_day) >= 3


def test_resume_skips_existing_keys():
    drv, clock = _driver(utc(2026, 11, 18, 0))
    work = _work(10)
    landed = {c.slot for c in work[:7]}
    sent = []
    n = drv.run(work, lambda c: c.slot in landed, lambda c: sent.append(c.slot) or True)
    assert n == 3 and sent == [c.slot for c in work[7:]]
