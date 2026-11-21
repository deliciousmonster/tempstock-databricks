"""A chunk body must match its chunk: every column as long as `time`, dates in the span, lead 7
present. `fetch_checked`, catch-up's fetch, counts a failed call and an invalid body apart."""

import json
from datetime import date, datetime, timedelta

import pytest

from tests.conftest import utc
from tests.unit.fakes import FakeClock
from weather_signal.ingest import backfill as bf
from weather_signal.ingest.fetch import RawResponse
from weather_signal.ingest.sources import DAILY_VARS, HOURLY_VARS

ERA5_YEAR = bf.Chunk("open-meteo-era5", date(1991, 1, 1), date(1991, 12, 31), 3)
PREV_14 = bf.Chunk("open-meteo-previous-runs", date(2021, 3, 24), date(2021, 4, 6), 8)


def era5_body(c: bf.Chunk, days: int | None = None) -> bytes:
    n = c.days if days is None else days
    times = [(c.start + timedelta(days=i)).isoformat() for i in range(n)]
    loc = {"daily": {"time": times, **{v: [1.0] * n for v in DAILY_VARS}}}
    return json.dumps([loc] * 50).encode()


def prev_body(c: bf.Chunk, edge: bool = False, lead0: float | None = 1.0) -> bytes:
    n = c.days * 24
    t0 = datetime.combine(c.start, datetime.min.time())
    times = [(t0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    hourly = {"time": times, **{v: [1.0] * n for v in HOURLY_VARS}, "temperature_2m": [lead0] * n}
    if edge:
        for lead in range(1, 8):
            hourly[f"temperature_2m_previous_day{lead}"][: lead * 24] = [None] * (lead * 24)
    return json.dumps([{"hourly": hourly}] * 50).encode()


def _server(status, body):
    clock = FakeClock(utc(2026, 11, 18, 1))
    return lambda url, params, timeout: RawResponse(status, {}, body, clock.now())


def _prev(mutate):
    doc = json.loads(prev_body(PREV_14, edge=True))
    for loc in doc:
        mutate(loc["hourly"])
    return json.dumps(doc).encode()


def _era5(mutate):
    doc = json.loads(era5_body(ERA5_YEAR))
    for loc in doc:
        mutate(loc["daily"])
    return json.dumps(doc).encode()


def _short_lead(h):
    h["temperature_2m_previous_day3"] = h["temperature_2m_previous_day3"][:10]


def _short_t2m(h):
    h["temperature_2m"] = h["temperature_2m"][:100]


def _lead7_empty(h):
    h["temperature_2m_previous_day7"] = [None] * len(h["time"])


def _prev_shifted(h):
    h["time"] = [t.replace("2021-", "2022-") for t in h["time"]]


def _era5_short_mean(d):
    d["temperature_2m_mean"] = d["temperature_2m_mean"][:50]


def _era5_other_year(d):
    d["time"] = [(date(2001, 1, 1) + timedelta(days=i)).isoformat() for i in range(len(d["time"]))]


@pytest.mark.parametrize(
    "body",
    [
        _prev(_short_lead),
        _prev(_short_t2m),
        _prev(_lead7_empty),
        _prev(_prev_shifted),
    ],
    ids=["lead 3 of 10 values", "temperature_2m of 100", "lead 7 null everywhere", "dates of 2022"],
)
def test_previous_runs_chunk_rejects_a_body_that_does_not_match(body):
    assert not bf.check(PREV_14, body).ok


@pytest.mark.parametrize(
    "body", [_era5(_era5_short_mean), _era5(_era5_other_year)], ids=["mean of 50", "dates of 2001"]
)
def test_era5_chunk_rejects_a_body_that_does_not_match(body):
    assert not bf.check(ERA5_YEAR, body).ok


def test_the_archive_edge_and_a_full_year_still_pass():
    assert bf.check(PREV_14, prev_body(PREV_14, edge=True)) == bf.validate.Verdict(
        True, flag="interior_null"
    )
    assert bf.check(ERA5_YEAR, era5_body(ERA5_YEAR)).ok


def _loc_prev(c):
    return json.loads(prev_body(c))[0]


def _malformed():
    def drop(key):
        loc = _loc_prev(PREV_14)
        del loc["hourly"][key]
        return [loc] * 50

    one_null = [_loc_prev(PREV_14)] * 49 + [None]
    return {
        "temperature_2m absent": drop("temperature_2m"),
        "time absent": drop("time"),
        "a location null": one_null,
        "hourly a list": [{"hourly": [1, 2]}] * 50,
        "a column a number": [{"hourly": {**_loc_prev(PREV_14)["hourly"], "temperature_2m": 3}}]
        * 50,
        "time not dates": [{"hourly": {**_loc_prev(PREV_14)["hourly"], "time": [7] * 336}}] * 50,
    }


@pytest.mark.parametrize("name", list(_malformed()))
def test_a_body_that_makes_a_validator_raise_is_invalid_not_a_crash(name):
    body = json.dumps(_malformed()[name]).encode()
    rep = bf.Report()
    assert bf.fetch_checked(PREV_14, _server(200, body), rep) is None
    assert (rep.invalid, rep.failed) == (1, 0)


def test_era5_body_missing_a_variable_is_invalid_not_a_crash():
    doc = json.loads(era5_body(ERA5_YEAR))
    del doc[0]["daily"]["temperature_2m_mean"]
    assert not bf.check(ERA5_YEAR, json.dumps(doc).encode()).ok


def test_a_failed_call_is_counted_failed_not_invalid():
    rep = bf.Report()
    assert bf.fetch_checked(ERA5_YEAR, _server(503, b""), rep) is None
    assert (rep.invalid, rep.failed) == (0, 1)


def test_the_archive_edge_is_fetched_with_its_flag():
    rep = bf.Report()
    got = bf.fetch_checked(PREV_14, _server(200, prev_body(PREV_14, edge=True)), rep)
    assert got is not None and got[1] == "interior_null"
    assert (rep.invalid, rep.failed) == (0, 0)
