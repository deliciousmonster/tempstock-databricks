"""The catch-up's newest session: unsettled is a retry, a real gap fails, and a failure fails the run.

Measured on dev 2026-11-19 01:05Z: Yahoo SPY's 2026-11-18 close was null four hours after the
close, the run logged `failed 1` and ended SUCCESS, since a wheel task ignores `main`'s return.
"""

import json
from datetime import UTC, date, datetime, time, timedelta

import pytest

from tests.unit.fakes import FakeStore
from tests.unit.test_catchup import N, seeded
from weather_signal import config
from weather_signal.ingest import catchup, hourly, validate, yahoo_history
from weather_signal.ingest.fetch import RawResponse
from weather_signal.ingest.files import Unreadable
from weather_signal.jobs import ingest as ingest_job

FETCHED = datetime(2026, 11, 19, 1, 5, tzinfo=UTC)


def _chart(days, closes):
    ts = [int(datetime.combine(d, time(14, 30), tzinfo=UTC).timestamp()) for d in days]
    result = {"timestamp": ts, "indicators": {"quote": [{"close": closes}]}}
    return json.dumps({"chart": {"result": [result]}}).encode()


def _days(cal):
    return cal.between(date(1993, 1, 29), cal.latest_closed_before(FETCHED))


def test_a_null_newest_close_is_settling_not_invalid(cal):
    days = _days(cal)
    assert days[-1] == date(2026, 11, 18)
    v = validate.history(_chart(days, [100.0] * (len(days) - 1) + [None]), FETCHED, cal, days[0])
    assert not v.ok and v.flag == validate.SETTLING and "2026-11-18" in v.reason


def test_a_null_close_before_the_newest_is_still_a_failure(cal):
    days = _days(cal)
    closes = [100.0] * len(days)
    closes[-2] = None
    v = validate.history(_chart(days, closes), FETCHED, cal, days[0])
    assert not v.ok and v.flag is None and "1 sessions missing" in v.reason


def test_a_settling_ticker_is_reported_and_not_counted_failed(monkeypatch, cal):
    rep = _settling_run(monkeypatch, cal, FETCHED)
    assert rep.failed == 0 and rep.landed == 0 and rep.left == []
    assert rep.settling and all("not settled" in x for x in rep.settling)


def test_attempt_maps_a_settling_verdict_to_settling_and_lands_nothing(monkeypatch, cal):
    days = cal.between(yahoo_history.start_for("SPY"), cal.latest_closed_before(FETCHED))
    body = _chart(days, [100.0] * (len(days) - 1) + [None])

    class Src:
        def __init__(self, transport):
            pass

        def fetch(self, ticker, start, end):
            return RawResponse(200, {}, body, FETCHED)

    monkeypatch.setattr(yahoo_history.sources, "YahooChartDaily", Src)
    s = seeded()
    before = dict(s.files)
    got, why = yahoo_history.attempt_for(s, N, None, cal, FETCHED)("SPY")
    assert got == "settling" and "not settled" in why
    assert s.files == before


@pytest.fixture
def ingest_main(monkeypatch):
    """`weather-signal-ingest` with every workspace and network call replaced; the catch-up report is set per test."""
    rep = catchup.Report()
    monkeypatch.setattr(ingest_job.Files, "from_sdk", staticmethod(lambda *a: None))
    monkeypatch.setattr(ingest_job, "RequestsTransport", lambda: None)
    monkeypatch.setattr(ingest_job, "load_calendar", lambda: None)
    monkeypatch.setattr(hourly, "weather_hour", lambda *a: "skipped")
    monkeypatch.setattr(catchup, "run", lambda *a: rep)
    return rep


def test_a_failed_catch_up_raises_so_the_wheel_task_fails(ingest_main):
    ingest_main.failed = 1
    with pytest.raises(ingest_job.CatchUpFailed, match="1 failed"):
        ingest_job.main(["--catch-up", "true", "--skip-alphavantage"])


def test_a_settling_catch_up_returns_and_the_run_succeeds(ingest_main):
    ingest_main.settling = ["yahoo SPY: newest session 2026-11-18 not settled"]
    assert ingest_job.main(["--catch-up", "true", "--skip-alphavantage"]) == 0


def _settling_run(monkeypatch, cal, now):
    monkeypatch.setattr(
        yahoo_history,
        "attempt_for",
        lambda *a: lambda t: ("settling", "newest session not settled"),
    )
    monkeypatch.setattr(catchup.backfill, "fetch_checked", lambda c, t, r: None)
    s = seeded()
    for src in ("open-meteo-era5", "open-meteo-previous-runs"):
        s.files = {k: v for k, v in s.files.items() if f"/{src}/" not in k}
    return catchup.run(s, N, None, cal, now, lambda: now, lambda w: None)


def test_a_stale_seed_with_an_unsettled_newest_close_is_settling_not_a_failure(monkeypatch, cal):
    """SPY landed through 2026-11-17, caught up on 2026-11-24 four hours
    after the 11-23 close; only that close is unsettled, so the run leaves it for the next ingest."""
    rep = _settling_run(monkeypatch, cal, datetime(2026, 11, 24, 1, 5, tzinfo=UTC))
    assert rep.failed == 0 and rep.left == []
    assert any("SPY" in x for x in rep.settling)


def _daily_settling(monkeypatch, cal, days, s=None):
    """One catch-up a day at 01:05 UTC from 2026-11-24 against a feed whose newest close never
    settles; the reports in order, on one volume (`s`, given the seeded files)."""
    monkeypatch.setattr(
        yahoo_history,
        "attempt_for",
        lambda *a: lambda t: ("settling", "newest session not settled"),
    )
    monkeypatch.setattr(catchup.backfill, "fetch_checked", lambda c, t, r: None)
    s = FakeStore() if s is None else s
    s.files = {k: v for k, v in seeded().files.items() if "/open-meteo-" not in k}
    start = datetime(2026, 11, 24, 1, 5, tzinfo=UTC)
    out = []
    for d in range(days):
        now = start + timedelta(days=d)
        out.append(catchup.run(s, N, None, cal, now, lambda t=now: t, lambda w: None))
    return out


def test_a_feed_that_never_settles_fails_after_three_sessions(monkeypatch, cal):
    """The age limit could never fire, so a null newest close passed forever."""
    reps = _daily_settling(monkeypatch, cal, 330)
    spy = [any("SPY" in x for x in r.left) for r in reps]
    first = spy.index(True)
    assert first <= 5, "SPY failed only after more than three sessions"
    assert all(spy[first:]), "a later run passed the same unsettled feed again"
    assert not any(spy[:first]) and all(any("SPY" in x for x in r.settling) for r in reps[:first])


def test_one_session_seen_twice_counts_once(cal):
    s = seeded()
    for _ in range(5):
        n = catchup.unsettled_sessions(s, N, "SPY", date(2026, 11, 17), date(2026, 11, 23))
    assert n == 1


def test_a_landing_resets_the_count_and_prunes_the_marks_before_it(cal):
    """Nothing deleted a mark, so settling/ grew one file per stale session."""
    s = seeded()
    for due in (date(2026, 11, 18), date(2026, 11, 19), date(2026, 11, 20)):
        catchup.unsettled_sessions(s, N, "SPY", date(2026, 11, 17), due)
    assert catchup.unsettled_sessions(s, N, "SPY", date(2026, 11, 20), date(2026, 11, 23)) == 1
    root = f"{N.ledger}/settling/yahoo/SPY"
    assert [k for k in s.files if k.startswith(root)] == [f"{root}/2026-11-23"]


def test_an_unreadable_settling_folder_fails_its_ticker_and_the_rest_still_run(monkeypatch, cal):
    """One list that raised Unreadable ended the pass before the later tickers."""

    class Flaky(FakeStore):
        def list(self, path):
            if path.endswith("/settling/yahoo/SPY"):
                raise Unreadable(path)
            return super().list(path)

    (rep,) = _daily_settling(monkeypatch, cal, 1, Flaky())
    assert [x for x in rep.left if "SPY" in x] == [
        f"yahoo SPY: {N.ledger}/settling/yahoo/SPY not readable"
    ]
    assert {f"yahoo {t}" for t in config.TICKERS} <= set(rep.ends)
    assert len(rep.left) + len(rep.settling) == len(config.TICKERS)


def test_a_stray_file_in_a_settling_folder_is_not_a_mark():
    """A name that is not a date raised ValueError out of the count."""
    s = seeded()
    s.files[f"{N.ledger}/settling/yahoo/SPY/.notes"] = b"x"
    assert catchup.unsettled_sessions(s, N, "SPY", date(2026, 11, 17), date(2026, 11, 23)) == 1


def test_a_settling_mark_that_was_not_written_fails_the_ticker(monkeypatch, cal):
    """The mark's write result was ignored, so with every ledger write failing the
    count never grew and a stuck feed never failed."""

    class DeadWrites(FakeStore):
        def put_if_absent(self, path, data):
            return "unknown" if "/settling/" in path else super().put_if_absent(path, data)

    (rep,) = _daily_settling(monkeypatch, cal, 1, DeadWrites())
    assert rep.settling == [] and rep.failed == len(config.TICKERS)
    assert f"yahoo SPY: {N.ledger}/settling/yahoo/SPY/2026-11-23 not written" in rep.left


class _AtEra5Hour(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 19, hourly.ERA5_HOUR, 5, tzinfo=UTC)


def test_a_failed_scheduled_era5_hour_catch_up_fails_the_run(monkeypatch, ingest_main):
    monkeypatch.setattr(ingest_job, "datetime", _AtEra5Hour)
    monkeypatch.setattr(hourly, "era5_day", lambda *a: "skipped")
    ingest_main.failed = 1
    with pytest.raises(ingest_job.CatchUpFailed, match="1 failed"):
        ingest_job.main(["--skip-alphavantage"])


def test_a_clean_scheduled_era5_hour_catch_up_succeeds(monkeypatch, ingest_main):
    monkeypatch.setattr(ingest_job, "datetime", _AtEra5Hour)
    monkeypatch.setattr(hourly, "era5_day", lambda *a: "skipped")
    ingest_main.settling = ["yahoo SPY: newest session 2026-11-18 not settled"]
    assert ingest_job.main(["--skip-alphavantage"]) == 0
