"""Validate before landing: what must land, what lands flagged, what never lands."""

import copy
import json
from datetime import UTC, date, datetime, time

from tests.conftest import fixture_bytes, fixture_json, utc
from weather_signal.ingest import validate


def _many(doc, n=50):
    return json.dumps([copy.deepcopy(doc) for _ in range(n)]).encode()


def _era5(mutate=None, n=50):
    doc = fixture_json("era5-now.json")
    if mutate:
        mutate(doc["daily"])
    return validate.era5_steady(_many(doc, n), days=15)


def test_era5_trailing_nulls_land_unflagged():
    v = _era5()
    assert v.ok and v.flag is None


def test_previous_runs_payload_lands():
    body = _many(fixture_json("prevruns-1807.json"))
    assert validate.previous_runs(body, utc(2026, 11, 17, 18, 7), 30).ok


def test_interior_null_lands_flagged():
    def hole(d):
        d["temperature_2m_mean"][2] = None

    v = _era5(hole)
    assert v.ok and v.flag == "interior_null"


def test_49_locations_rejected():
    assert not _era5(n=49).ok
    body = _many(fixture_json("prevruns-1807.json"), 49)
    assert not validate.previous_runs(body, utc(2026, 11, 17, 18, 7), 30).ok


def test_suffix_of_eleven_days_rejected():
    def long(d):
        d["temperature_2m_max"][4:] = [None] * 11

    v = _era5(long)
    assert not v.ok and "11" in v.reason


def test_rate_note_and_html_rejected(cal):
    note = b'{"Note": "Thank you for using Alpha Vantage! Our standard API rate limit is 25 requests per day."}'
    assert not validate.prices(note, utc(2026, 11, 17, 20), cal).ok
    assert not validate.prices(fixture_bytes("stooq-pow.html"), utc(2026, 11, 17, 20), cal).ok


def test_price_bodies_land(cal):
    for name, at in (
        ("av-spy-compact.json", utc(2026, 11, 17, 20, 28)),
        ("av-wmt-compact.json", utc(2026, 11, 20, 14, 8)),
        ("yahoo-wmt-recent.json", utc(2026, 11, 19, 21, 16)),
    ):
        assert validate.prices(fixture_bytes(name), at, cal).ok, name


NOW = utc(2026, 11, 18, 4, 20)


def _chart(days, closes=None):
    ts = [int(datetime.combine(d, time(14, 30), tzinfo=UTC).timestamp()) for d in days]
    quote = {"close": closes or [100.0] * len(ts)}
    result = {"meta": {"symbol": "X"}, "timestamp": ts, "indicators": {"quote": [quote]}}
    return json.dumps({"chart": {"result": [result]}}).encode()


def _from(cal, first):
    return cal.between(first, cal.latest_closed_before(NOW))


def test_a_history_from_the_first_session_after_a_holiday_start_lands(cal):
    assert validate.history(_chart(_from(cal, date(1991, 1, 2))), NOW, cal, date(1991, 1, 1)).ok


def test_a_benchmark_from_its_first_session_lands_and_one_day_late_does_not(cal):
    start = date(1993, 1, 29)
    assert validate.history(_chart(_from(cal, start)), NOW, cal, start).ok
    late = validate.history(_chart(_from(cal, date(1993, 2, 1))), NOW, cal, start)
    assert not late.ok and "1993-02-01" in late.reason and "1993-01-29" in late.reason


def test_a_45_percent_step_is_refused_and_a_39_percent_one_lands(cal):
    days = _from(cal, date(1991, 1, 2))
    i = days.index(date(2000, 3, 1))
    for step, ok in ((1.45, False), (0.55, False), (1.39, True)):
        closes = [100.0] * i + [100.0 * step] * (len(days) - i)
        got = validate.history(_chart(days, closes), NOW, cal, date(1991, 1, 1))
        assert got.ok is ok, (step, got.reason)
        assert ok or "2000-03-01" in got.reason


def test_a_null_close_an_all_null_body_and_an_off_session_bar_are_refused_naming_the_date(cal):
    start, days = date(1991, 1, 1), _from(cal, date(1991, 1, 2))
    i = days.index(date(1998, 11, 30))
    one_null = [100.0] * len(days)
    one_null[i] = None
    got = validate.history(_chart(days, one_null), NOW, cal, start)
    assert not got.ok and "1998-11-30" in got.reason
    assert not validate.history(_chart(days, [None] * len(days)), NOW, cal, start).ok
    saturday = sorted([*days, date(1995, 7, 1)])
    got = validate.history(_chart(saturday), NOW, cal, start)
    assert not got.ok and "1995-07-01" in got.reason


def test_a_body_from_before_the_requested_start_is_refused(cal):
    got = validate.history(_chart(_from(cal, date(1993, 1, 4))), NOW, cal, date(1993, 1, 29))
    assert not got.ok and "1993-01-04" in got.reason
