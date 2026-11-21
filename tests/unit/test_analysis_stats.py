"""A planted lagged relationship is found; on noise, false positives stay at 10% or less."""

import numpy as np
import pandas as pd

from weather_signal.research import stats


def test_planted_relationship_found():
    rng = np.random.default_rng(7)
    x = pd.Series(rng.normal(size=600))
    y = pd.Series(0.4 * np.roll(x.to_numpy(), 2) + rng.normal(size=600))
    assert stats.granger_significant(x, y, 5)
    xc = stats.lagged_xcorr(x, y, max_lag=4, n_boot=100)
    row = xc[xc["lag"] == 2].iloc[0]
    assert row["corr"] > row["band_hi"]


def test_noise_false_positives_at_most_10_percent():
    hits = 0
    for seed in range(200):
        rng = np.random.default_rng(seed)
        x, y = pd.Series(rng.normal(size=250)), pd.Series(rng.normal(size=250))
        hits += stats.granger_significant(x, y, 5)
    assert hits <= 20


def test_residualize_uses_only_the_past():
    rng = np.random.default_rng(3)
    x = pd.DataFrame({"m": rng.normal(size=300)})
    y = 2 * x["m"] + 0.01 * rng.normal(size=300)
    r = stats.residualize(y, x, window=100)
    assert len(r) == 200 and r.abs().max() < 0.1
