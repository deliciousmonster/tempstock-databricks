"""Forecast `known_at = v - 24N h + P`: nothing kept is younger than the boundary, no fill kept.

The 17:55 and 18:24 payloads were not kept by the reviewers; 18:07, 18:35 and 18:41 stand in.
"""

import pytest

from tests.conftest import fixture_json, utc
from weather_signal import asof, config

SAMPLES = {"1807": (18, 7), "1835": (18, 35), "1841": (18, 41)}


def _fill(body):
    h = body["hourly"]
    fill = set()
    for n in asof.LEADS:
        vals, k = h[f"temperature_2m_previous_day{n}"], len(h["time"])
        while k > 0 and (vals[k - 1] is None or vals[k - 1] == h["temperature_2m"][k - 1]):
            if vals[k - 1] is not None:
                fill.add((h["time"][k - 1], n))
            k -= 1
    return fill


@pytest.mark.parametrize("name", SAMPLES)
def test_no_kept_value_inside_the_boundary(name):
    body, fetched = fixture_json(f"prevruns-{name}.json"), utc(2026, 11, 17, *SAMPLES[name])
    kept = asof.landed_values(body, fetched, config.P_PROVISIONAL_H)
    assert kept
    for v, n, _ in kept:
        assert (fetched - (v - asof.timedelta(hours=24 * n))).total_seconds() / 3600 > 16.4


@pytest.mark.parametrize("name", SAMPLES)
def test_every_fill_hour_dropped(name):
    body, fetched = fixture_json(f"prevruns-{name}.json"), utc(2026, 11, 17, *SAMPLES[name])
    fill = _fill(body)
    assert fill
    kept = {
        (v.strftime("%Y-%m-%dT%H:%M"), n)
        for v, n, _ in asof.landed_values(body, fetched, config.P_PROVISIONAL_H)
    }
    assert not kept & fill
