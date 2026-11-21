"""Index normals over 1961 to 1990, every year present, or the training half when one is missing."""

from datetime import date, timedelta

import pandas as pd

from weather_signal.signal import temperature_index


def _years(doy, years, value):
    """Rows on day of year `doy`, which is a different calendar date in a leap year."""
    return [(date(y, 1, 1) + timedelta(days=doy - 1), value) for y in years]


def _normals(rows):
    return temperature_index.normals(pd.DataFrame(rows, columns=["date", "index_c"])).set_index(
        "day_of_year"
    )


def test_normals_window_is_1961_to_1990_and_a_scored_year_changes_nothing():
    base = _years(100, range(1961, 1991), 1.0)
    late = _years(100, range(1991, 2026), 100.0) + _years(100, range(1950, 1961), -100.0)
    assert temperature_index.NORMALS_YEARS == (1961, 1990)
    assert _normals(base).loc[100, "normal_c"] == _normals(base + late).loc[100, "normal_c"] == 1.0


def test_a_day_missing_one_window_year_has_no_normal():
    n = _normals(_years(100, [y for y in range(1961, 1991) if y != 1975], 1.0))
    assert n.loc[100, "n_years"] == 29 and pd.isna(n.loc[100, "normal_c"])


def test_day_366_needs_its_seven_leap_years_only():
    leap = [y for y in range(1961, 1991) if y % 4 == 0]
    assert (
        temperature_index.expected_years(366) == len(leap) == 7
        and temperature_index.expected_years(365) == 30
    )
    assert _normals(_years(366, leap, 2.0)).loc[366, "normal_c"] == 2.0
    assert pd.isna(_normals(_years(366, leap[1:], 2.0)).loc[366, "normal_c"])
