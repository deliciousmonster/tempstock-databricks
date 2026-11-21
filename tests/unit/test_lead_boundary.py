"""`lead_boundary` on the recorded payloads and `P` from the samples."""

import pytest

from tests.conftest import fixture_json, utc
from weather_signal import asof

SAMPLES = {"1807": (18, 7), "1835": (18, 35), "1841": (18, 41)}


@pytest.mark.parametrize("name", SAMPLES)
def test_boundaries(name):
    b = asof.lead_boundary(fixture_json(f"prevruns-{name}.json"), utc(2026, 11, 17, *SAMPLES[name]))
    assert all(13.1 <= b[n] <= 13.7 for n in (4, 5, 6, 7))
    assert 16.1 <= b[3] <= 16.7


def test_p_is_max_plus_two_rounded_up_without_floor():
    samples = []
    for name, t in SAMPLES.items():
        samples += asof.lead_boundary(
            fixture_json(f"prevruns-{name}.json"), utc(2026, 11, 17, *t)
        ).values()
    assert asof.p_from_samples(samples) == 19
    assert asof.p_from_samples([13.2, 16.4, 22.4, 15.0]) == 25
    assert asof.p_from_samples([22.0]) == 24
