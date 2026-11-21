"""The ingest catch-up: from each source's newest landed slot to now, inside the weighted budget."""

import gzip
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from tests.unit.fakes import FakeStore
from tests.unit.test_train import cal_of as train_cal
from weather_signal import config
from weather_signal.ingest import backfill, catchup, yahoo_history
from weather_signal.ingest.envelope import landing_path
from weather_signal.ingest.fetch import RawResponse

N = config.Names("workspace", "dev_")
NOW = datetime(2026, 12, 31, 5, 5, tzinfo=UTC)


def put(store, source, slot_time, slot, endpoint="https://x/v1/archive", name="k"):
    env = {"source": source, "slot": slot, "request": {"endpoint": endpoint}}
    store.files[landing_path(N.raw, source, slot_time, name)] = gzip.compress(
        json.dumps(env).encode()
    )


def seeded():
    """ERA5 through 2026-11-10, Previous Runs through 2026-11-16, an SPY history to 2026-11-17."""
    s = FakeStore()
    put(s, "open-meteo-era5", datetime(1961, 1, 1, tzinfo=UTC), "1961-01-01..2013-06-26", name="o")
    prev = "open-meteo-previous-runs"
    put(s, prev, datetime(2021, 3, 24, tzinfo=UTC), "2021-03-24..2026-11-15", name="o")
    put(s, "open-meteo-era5", datetime(2013, 6, 27, tzinfo=UTC), "2013-06-27..2026-06-23", name="a")
    put(s, "open-meteo-era5", datetime(2026, 6, 24, tzinfo=UTC), "2026-06-24..2026-11-10", name="b")
    put(s, "open-meteo-previous-runs", datetime(2026, 11, 17, 4, tzinfo=UTC), "2026-11-17T04:00:00")
    spy = "https://query1.finance.yahoo.com/v8/finance/chart/SPY"
    put(s, "yahoo", datetime(2013, 6, 27, tzinfo=UTC), "2013-06-27..2026-11-17", spy)
    return s


def faked(monkeypatch, now):
    """Fake every fetch to land; returns the lists the chunks and tickers fetched go into."""
    fetched, tried = [], []
    resp = RawResponse(200, {}, b"{}", now)
    monkeypatch.setattr(
        backfill, "fetch_checked", lambda c, t, r: fetched.append(c) or (resp, None)
    )
    monkeypatch.setattr(
        yahoo_history, "attempt_for", lambda *a: lambda t: tried.append(t) or ("valid", "p")
    )
    return fetched, tried


def cal():
    return train_cal(sorted(date(2026, 12, 30) - timedelta(days=i) for i in range(5)))


@pytest.mark.parametrize(
    ("slot", "end"),
    [("2013-06-27..2026-11-10", date(2026, 11, 10)), ("2026-11-17", date(2026, 11, 16)),
     ("2026-11-17T04:00:00+00:00", date(2026, 11, 16))],
)  # fmt: skip
def test_a_slot_ends_where_its_data_does(slot, end):
    assert catchup.slot_end(slot) == end


def test_the_newest_end_is_read_from_the_newest_folder_per_source_and_ticker():
    s = seeded()
    assert catchup.newest(s, N.raw, "open-meteo-era5") == date(2026, 11, 10)
    assert catchup.newest(s, N.raw, "open-meteo-previous-runs") == date(2026, 11, 16)
    assert catchup.newest(s, N.raw, "yahoo", "SPY") == date(2026, 11, 17)
    assert catchup.newest(s, N.raw, "yahoo", "WMT") is None


def test_the_plan_starts_the_day_after_the_newest_end_and_reaches_now():
    ends = {"open-meteo-era5": date(2026, 11, 10), "open-meteo-previous-runs": date(2026, 11, 16)}
    work = catchup.weather_plan(ends, NOW.date())
    era5 = [c for c in work if c.source == "open-meteo-era5"]
    prev = [c for c in work if c.source == "open-meteo-previous-runs"]
    assert era5[0].start == date(2026, 11, 11) and era5[-1].end == NOW.date() - catchup.ERA5_LAG
    assert prev[0].start == date(2026, 11, 17)
    assert prev[-1].end == NOW.date() - catchup.PREVIOUS_RUNS_LAG


def test_the_plan_stops_at_the_budget_and_skips_a_source_with_nothing_landed():
    ends = {"open-meteo-era5": date(2000, 1, 1), "open-meteo-previous-runs": None}
    work = catchup.weather_plan(ends, NOW.date())
    assert sum(c.cost for c in work) <= catchup.BUDGET < sum(c.cost for c in work) + work[0].cost
    assert {c.source for c in work} == {"open-meteo-era5"}


def test_a_seeded_gap_is_fetched_through_now(monkeypatch):
    """Seeded through November, run on 2026-12-31: every gap chunk lands and stale tickers refetch."""
    s = seeded()
    fetched, tried = faked(monkeypatch, NOW)
    rep = catchup.run(s, N, None, cal(), NOW, lambda: NOW, lambda w: None)
    era5 = [c for c in fetched if c.source == "open-meteo-era5"]
    prev = [c for c in fetched if c.source == "open-meteo-previous-runs"]
    assert era5[0].start == date(2026, 11, 11) and era5[-1].end == date(2026, 12, 23)
    assert prev[0].start == date(2026, 11, 17) and prev[-1].end >= date(2026, 12, 15)
    assert sorted(tried) == sorted(config.TICKERS)  # SPY ends 2026-11-17, before 2026-12-30
    assert rep.failed == 0 and rep.landed == len(fetched) + len(tried)
    assert all(s.exists(backfill.landing(N.raw, c)) == "present" for c in fetched)


def test_a_ticker_current_through_the_last_close_is_not_fetched(monkeypatch):
    s = seeded()
    spy = "https://query1.finance.yahoo.com/v8/finance/chart/SPY"
    put(s, "yahoo", datetime(2013, 6, 27, tzinfo=UTC), "1993-01-29..2026-12-30", spy, name="z")
    monkeypatch.setattr(backfill, "fetch_checked", lambda c, t, r: None)
    tried = []
    monkeypatch.setattr(
        yahoo_history, "attempt_for", lambda *a: lambda t: tried.append(t) or ("valid", "p")
    )
    catchup.run(s, N, None, cal(), NOW, lambda: NOW, lambda w: None)
    assert "SPY" not in tried


def test_a_gap_past_the_budget_is_deferred_not_failed(monkeypatch):
    s = FakeStore()
    put(s, "open-meteo-era5", datetime(2000, 1, 1, tzinfo=UTC), "1991-01-01..2000-01-01")
    resp = RawResponse(200, {}, b"{}", NOW)
    monkeypatch.setattr(backfill, "fetch_checked", lambda c, t, r: (resp, None))
    monkeypatch.setattr(yahoo_history, "attempt_for", lambda *a: lambda t: ("valid", "p"))
    ends = {"open-meteo-era5": date(2000, 1, 1)}
    gap = len(catchup.weather_gap(ends, NOW.date())) + len(catchup.holes(s, N.raw, ends))
    rep = catchup.run(s, N, None, cal(), NOW, lambda: NOW, lambda w: None)
    assert rep.failed == 0 and rep.deferred > 0
    assert rep.planned + rep.deferred - len(config.TICKERS) == gap
