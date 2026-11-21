"""Catch-up fills holes inside each source's history, from its start, and fetches nothing twice."""

from datetime import UTC, date, datetime, timedelta

import pytest

from tests.unit.fakes import FakeStore
from tests.unit.test_catchup import N, faked, put
from tests.unit.test_train import cal_of
from weather_signal import config
from weather_signal.ingest import backfill, catchup, yahoo_history

NOW = datetime(2026, 11, 20, 23, 5, tzinfo=UTC)
ERA5, PREV = "open-meteo-era5", "open-meteo-previous-runs"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}"


def cal():
    return cal_of(sorted(date(2026, 11, 20) - timedelta(days=i) for i in range(5)))


def holed():
    """The archive's shape: ERA5 without 1986 or 2011-12-27..2013-06-26, every ticker current."""
    s = FakeStore()
    for slot in ("1961-01-01..1985-12-31", "1987-01-01..2011-12-26", "2013-06-27..2026-11-12"):
        put(s, ERA5, datetime.fromisoformat(slot[:10]).replace(tzinfo=UTC), slot, name=slot[:4])
    put(s, PREV, datetime(2021, 3, 24, tzinfo=UTC), "2021-03-24..2026-11-18")
    for t in config.TICKERS:
        start = yahoo_history.start_for(t)
        put(s, "yahoo", datetime(start.year, 1, 1, tzinfo=UTC), f"{start}..2026-11-20",
            YAHOO.format(t), name=t)  # fmt: skip
    return s


def run(s, monkeypatch):
    """One catch-up on `s` with fakes; returns the chunks and tickers it fetched."""
    fetched, tried = faked(monkeypatch, NOW)
    rep = catchup.run(s, N, None, cal(), NOW, lambda: NOW, lambda w: None)
    return fetched, tried, rep


def test_a_hole_inside_the_era5_history_is_fetched_and_then_not_again(monkeypatch):
    s = holed()
    fetched, tried, rep = run(s, monkeypatch)
    assert [c.slot for c in fetched] == [
        "1986-01-01..1986-12-31", "2011-12-27..2012-12-25", "2012-12-26..2013-06-26",
    ]  # fmt: skip
    assert rep.holes == [f"{ERA5} {c.slot}" for c in fetched] and not tried
    assert rep.landed == 3 and rep.failed == rep.deferred == 0
    again, tried, rep = run(s, monkeypatch)
    assert again == [] and tried == [] and rep.holes == [] and rep.planned == 0


def test_a_history_with_no_holes_fetches_nothing(monkeypatch):
    s = holed()
    put(s, ERA5, datetime(1986, 1, 1, tzinfo=UTC), "1986-01-01..1986-12-31", name="x")
    put(s, ERA5, datetime(2011, 12, 27, tzinfo=UTC), "2011-12-27..2013-06-26", name="y")
    fetched, tried, rep = run(s, monkeypatch)
    assert fetched == [] and tried == [] and rep.holes == [] and rep.planned == 0


def test_a_price_history_that_starts_late_is_fetched_whole(monkeypatch):
    s = holed()
    put(s, "yahoo", datetime(2013, 6, 27, tzinfo=UTC), "2013-06-27..2026-11-20",
        YAHOO.format("WMT"), name="WMT")  # fmt: skip
    del s.files[f"{N.raw}/yahoo/1991/01/01/00/WMT.json.gz"]
    _, tried, rep = run(s, monkeypatch)
    assert tried == ["WMT"] and rep.holes[-1] == "yahoo WMT from 1991-01-01"


def test_the_tail_is_planned_before_the_holes_and_both_fit_the_budget():
    tail = {ERA5: date(2026, 11, 10), PREV: date(2026, 11, 16)}
    filling = list(backfill.chunks(ERA5, date(1986, 1, 1), date(1990, 12, 31), 365, 3))
    work = catchup.weather_plan(tail, NOW.date(), filling)
    assert [c.start for c in work[:2]] == [date(2026, 11, 11), date(2026, 11, 17)]
    assert work[2:] == filling[: len(work) - 2] and sum(c.cost for c in work) <= catchup.BUDGET


def test_missing_days_between_spans_are_the_holes():
    d = date
    held = [
        (d(2000, 1, 5), d(2000, 1, 9)),
        (d(2000, 1, 7), d(2000, 1, 12)),
        (d(2000, 1, 20), d(2000, 2, 1)),
    ]
    assert catchup.missing(held, d(2000, 1, 1), d(2000, 1, 25)) == [
        (d(2000, 1, 1), d(2000, 1, 4)), (d(2000, 1, 13), d(2000, 1, 19))
    ]  # fmt: skip
    assert catchup.missing(held, d(2000, 1, 5), d(2000, 1, 12)) == []


def test_a_steady_hour_holds_the_day_before_its_folder():
    s = FakeStore()
    s.files[f"{N.raw}/{PREV}/2026/11/17/21/k.json.gz"] = b"never read"
    assert catchup.spans(s, N.raw, PREV) == [(date(2026, 11, 16), date(2026, 11, 16))]


def test_a_hole_whose_landing_does_not_answer_fails_the_run(monkeypatch):
    s = holed()
    era5_1986 = backfill.Chunk(ERA5, date(1986, 1, 1), date(1986, 12, 31), 3)
    s.unknown_puts.add(backfill.landing(N.raw, era5_1986))
    fetched, tried, rep = run(s, monkeypatch)
    assert len(fetched) == 3 and rep.landed == 2 and rep.failed == 1
    assert rep.left == [f"{ERA5} 1986-01-01..1986-12-31: landing did not answer"]


@pytest.mark.parametrize(("head", "landed", "failed"), [("present", 3, 0), ("unknown", 2, 1)])
def test_a_landing_that_lost_its_reply_is_checked_once(monkeypatch, head, landed, failed):
    s = holed()
    path = backfill.landing(N.raw, backfill.Chunk(ERA5, date(1986, 1, 1), date(1986, 12, 31), 3))
    real, heads = s.put_if_absent, []

    def lost_reply(p, data):
        got = real(p, data)
        if p != path:
            return got
        if head == "unknown":
            s.unknown_heads.add(path)
        return "unknown"

    monkeypatch.setattr(s, "put_if_absent", lost_reply)
    monkeypatch.setattr(s, "exists", lambda p, e=s.exists: heads.append(p) or e(p))
    fetched, tried, rep = run(s, monkeypatch)
    assert (rep.landed, rep.failed) == (landed, failed) and len(fetched) == 3
    assert heads.count(path) == 2  # the driver's skip probe, then the one check after the put
