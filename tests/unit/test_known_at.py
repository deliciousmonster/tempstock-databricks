"""The ERA5 backfill `known_at` constant."""

from datetime import date

from tests.conftest import utc
from weather_signal import asof


def test_era5_backfill_constant_is_eight_days():
    assert asof.known_at_era5_backfill(date(2026, 11, 1)) == utc(2026, 11, 9)
