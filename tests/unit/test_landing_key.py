"""The landing key is the request and its slot: distinct requests never share a path."""

from tests.conftest import utc
from weather_signal.ingest import envelope


def test_fifty_requests_fifty_paths():
    slot_time = utc(2026, 11, 18, 7)
    paths = {
        envelope.landing_path(
            "/r",
            "open-meteo-era5",
            slot_time,
            envelope.landing_key("open-meteo-era5", "e", {"start_date": f"1991-01-{i:02d}"}, "s"),
        )
        for i in range(1, 51)
    }
    assert len(paths) == 50


def test_two_writers_same_request_and_slot_one_path():
    a = envelope.landing_key("s", "e", {"a": "1", "b": "2"}, "2026-11-18T07:00:00+00:00")
    b = envelope.landing_key("s", "e", {"b": "2", "a": "1"}, "2026-11-18T07:00:00+00:00")
    assert a == b


def test_credentials_do_not_change_the_key():
    a = envelope.landing_key("av", "e", {"symbol": "WMT", "apikey": "one"}, "s")
    b = envelope.landing_key("av", "e", {"symbol": "WMT", "apikey": "two"}, "s")
    assert a == b
