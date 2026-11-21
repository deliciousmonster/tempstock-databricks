"""Decision day `D(as_of)` and horizons at `P` = 24, 28, 30."""

from datetime import date, timedelta

import pytest

from tests.conftest import utc
from weather_signal import asof

DAY = date(2026, 11, 18)


def _hours(p):
    return {h: asof.decision_day(utc(2026, 11, 18, h, 30), p) for h in range(24)}


@pytest.mark.parametrize(("p", "early"), [(24, 0), (28, 3), (30, 5)])
def test_previous_day_hours(p, early):
    m = _hours(p)
    assert [h for h, d in m.items() if d == DAY - timedelta(days=1)] == list(range(early))
    assert all(d == DAY for h, d in m.items() if h >= early)


@pytest.mark.parametrize("p", [24, 28, 30, 40, 46])
def test_2130_is_its_own_date_below_46_5(p):
    assert asof.decision_day(utc(2026, 11, 18, 21, 30), p) == DAY


@pytest.mark.parametrize("p", [24, 28, 30])
def test_lead_k_plus_2_last_hour_visible_all_decision_day(p):
    for h in range(24):
        as_of = utc(2026, 11, 18, h, 30)
        d = asof.decision_day(as_of, p)
        for k in asof.horizons(as_of, p):
            last = asof.target_date(d, k)
            v = utc(last.year, last.month, last.day, 23)
            assert asof.known_at_forecast(v, asof.fitted_lead(k), p) <= as_of, (h, k)


def test_horizons():
    for h in range(24):
        hs = asof.horizons(utc(2026, 11, 18, h, 30), 30)
        assert hs == (list(range(1, 7)) if h <= 4 else list(range(7))), h
    assert all(asof.horizons(utc(2026, 11, 18, h, 30), 24) == list(range(7)) for h in range(24))


def test_fractional_max_gives_whole_hour_p():
    p = asof.p_from_samples([22.6])
    assert p == 25
    k = asof.known_at_forecast(utc(2026, 11, 20, 7), 3, p)
    assert (k.minute, k.second) == (0, 0)
