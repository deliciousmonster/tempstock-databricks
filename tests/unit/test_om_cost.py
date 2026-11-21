"""Open-Meteo weighted cost, `locations x max(1, days/14 x vars/10)`."""

import pytest

from weather_signal.ingest.om_budget import weighted_cost


@pytest.mark.parametrize(
    ("locs", "days", "vars_", "cost"),
    [
        (50, 9, 8, 50.0),  # the hourly Previous Runs call: floor, not 25.7
        (1, 1, 1, 1.0),
        (1, 14, 10, 1.0),
        (1, 28, 10, 2.0),
    ],
)
def test_cost_formula(locs, days, vars_, cost):
    assert weighted_cost(locs, days, vars_) == pytest.approx(cost)


def test_era5_chunk_cost_is_above_the_floor():
    assert weighted_cost(50, 365, 3) == pytest.approx(50 * 365 / 14 * 3 / 10)
