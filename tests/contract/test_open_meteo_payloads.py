"""Recorded Open-Meteo archive and Previous Runs payloads parse; an all-null column is not data."""

from tests.conftest import fixture_json
from weather_signal.ingest import sources


def test_archive_payload_parses():
    locs = fixture_json("era5-archive-sample.json")
    assert len(locs) == 3
    cols = sources.daily_columns(locs[0])
    assert list(cols) == ["temperature_2m_mean"] and len(cols["temperature_2m_mean"]) == 3


def test_previous_runs_payload_parses_all_seven_leads():
    cols = sources.hourly_columns(fixture_json("prevruns-1807.json"))
    assert sorted(cols) == list(range(1, 8))
    assert all(len(v) == 216 for v in cols.values())


def test_previous_day8_all_null_is_absent():
    cols = sources.hourly_columns(fixture_json("prevruns-day8.json"))
    assert sorted(cols) == [7]


def test_two_locations_in_one_body():
    locs = fixture_json("prevruns-2loc.json")
    assert len(locs) == 2 and all(
        sorted(sources.hourly_columns(x)) == list(range(1, 8)) for x in locs
    )
