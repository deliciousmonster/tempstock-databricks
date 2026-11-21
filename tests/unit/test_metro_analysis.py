"""The metro study's pure parts: normals see only the past, the null keeps each series intact and
breaks the pairing, and the subset scorer agrees with a direct correlation."""

from datetime import date
from itertools import combinations

import numpy as np
import pandas as pd
import pytest

from weather_signal.research import metro_holdout as mh
from weather_signal.research import metro_search as ms
from weather_signal.research.metro_data import (
    anomalies,
    day_of_year,
    forward_targets,
    lagged,
    normals_before,
)


def _obs(start: str, end: str, value, metros=("A", "B")) -> pd.DataFrame:
    days = pd.date_range(start, end, freq="D")
    return pd.DataFrame(
        [{"cbsa_code": m, "date": d, "t_mean": value(d, m)} for d in days for m in metros]
    )


def test_day_of_year_folds_feb_29_onto_feb_28():
    d = pd.DatetimeIndex(["2020-02-28", "2020-02-29", "2020-03-01", "2021-03-01", "2020-12-31"])
    assert day_of_year(d).tolist() == [59, 59, 60, 60, 365]


def test_normals_ignore_every_row_on_or_after_the_cutoff():
    before = _obs("2001-01-01", "2003-12-31", lambda d, m: 10.0)
    after = _obs("2004-01-01", "2006-12-31", lambda d, m: 1000.0)
    n = normals_before(pd.concat([before, after]), date(2004, 1, 1))
    assert np.allclose(n.table.to_numpy(), 10.0)
    assert n.n_years.to_dict() == {"A": 3, "B": 3}
    changed = pd.concat([before, after.assign(t_mean=-1000.0)])
    assert n.table.equals(normals_before(changed, date(2004, 1, 1)).table)


def test_normals_refuse_when_nothing_precedes_the_cutoff():
    with pytest.raises(ValueError, match="no observations before"):
        normals_before(_obs("2010-01-01", "2010-12-31", lambda d, m: 1.0), date(2010, 1, 1))


def test_anomaly_is_the_value_minus_the_metros_own_seasonal_normal():
    def season(d, m):
        return 10 * np.sin(2 * np.pi * day_of_year([d])[0] / 365) + (5 if m == "B" else 0)

    obs = _obs("2001-01-01", "2004-12-31", season)
    n = normals_before(obs, date(2004, 1, 1), half_window=0)
    warm = _obs("2004-01-01", "2004-12-31", lambda d, m: season(d, m) + 2.0)
    a = anomalies(warm, n)
    assert np.allclose(a.to_numpy(), 2.0)


def test_forward_targets_are_close_to_close_and_excess_over_the_benchmark():
    idx = pd.date_range("2020-01-01", periods=7, freq="B")
    closes = pd.DataFrame(
        {"WMT": [100, 110, 99, 99, 99, 99, 120], "SPY": [10, 11, 11, 11, 11, 11, 11], "XLP": 1.0},
        index=idx,
    )
    t = forward_targets(closes)
    assert t["stock_1d"].iloc[0] == pytest.approx(0.10)
    assert t["xs_spy_1d"].iloc[0] == pytest.approx(0.0)
    assert t["xs_xlp_5d"].iloc[1] == pytest.approx(120 / 110 - 1)
    assert t["stock_5d"].iloc[2:].isna().all() and t["stock_1d"].iloc[-1:].isna().all()


def test_lag_reads_the_calendar_day_lag_days_before_the_session():
    anom = pd.DataFrame({"A": np.arange(10.0)}, index=pd.date_range("2020-01-01", periods=10))
    s = pd.DatetimeIndex(["2020-01-08"])
    assert lagged(anom, s, 0, "level")["A"].iloc[0] == 7
    assert lagged(anom, s, 3, "level")["A"].iloc[0] == 4
    assert lagged(anom, s, 3, "change")["A"].iloc[0] == 1


def test_circular_shift_keeps_values_and_autocorrelation_and_breaks_the_pairing():
    rng = np.random.default_rng(1)
    x = np.cumsum(rng.normal(size=600)) * 0.1 + rng.normal(size=600)
    y = x + 0.1 * rng.normal(size=600)
    shifted = ms.circular_shift(y, 200)
    assert sorted(shifted) == sorted(y)
    ac = lambda v: np.corrcoef(v, np.roll(v, 1))[0, 1]  # noqa: E731
    assert ac(shifted) == pytest.approx(ac(y))
    assert abs(np.corrcoef(x, shifted)[0, 1]) < 0.5 < np.corrcoef(x, y)[0, 1]


def test_shift_offsets_stay_at_least_min_shift_from_zero_both_ways():
    offs = ms.shift_offsets(100, 5000, np.random.default_rng(0))
    assert offs.min() >= 30 and offs.max() <= 70
    with pytest.raises(ValueError, match="no offset"):
        ms.shift_offsets(60, 1, np.random.default_rng(0))


def test_subset_scorer_equals_the_direct_correlation_of_the_mean():
    rng = np.random.default_rng(2)
    x, y = rng.normal(size=(300, 8)), rng.normal(size=(300, 2))
    idx = np.array(list(combinations(range(8), 3)))
    got = ms.SubsetScorer(x, idx).corr(y)
    for i, sub in enumerate(idx):
        for k in range(2):
            assert got[i, k] == pytest.approx(np.corrcoef(x[:, sub].mean(axis=1), y[:, k])[0, 1])


def test_subset_scorer_ranks_a_planted_pair_first():
    rng = np.random.default_rng(3)
    x = rng.normal(size=(800, 12))
    y = (0.3 * x[:, [4, 9]].mean(axis=1) + rng.normal(size=800))[:, None]
    idx = np.array(list(combinations(range(12), 2)))
    best = idx[np.abs(ms.SubsetScorer(x, idx).corr(y)[:, 0]).argmax()]
    assert best.tolist() == [4, 9]


def test_null_share_counts_the_real_run():
    assert ms.null_share(0.5, np.array([0.1, 0.2, 0.3])) == 0.25
    assert ms.null_share(0.0, np.array([0.1, 0.2, 0.3])) == 1.0


def test_lasso_matches_soft_threshold_on_an_orthonormal_design():
    c = np.array([0.5, -0.2, 0.05])
    b = ms.lasso_cd(np.eye(3), c, 0.1)
    assert b == pytest.approx([0.4, -0.1, 0.0])


def test_lasso_cv_keeps_the_planted_column_and_little_else():
    rng = np.random.default_rng(4)
    x = rng.normal(size=(1000, 30))
    y = 0.5 * x[:, 7] + rng.normal(size=1000)
    cv = ms.LassoCV(x)
    r2, alphas = cv.cv_r2(y)
    b = cv.fit(y, float(alphas[r2.argmax()]))
    assert r2.max() > 0.1 and abs(b[7]) == np.abs(b).max()
    noise_r2, _ = cv.cv_r2(rng.normal(size=1000))
    assert noise_r2.max() < 0.02


def test_frozen_picks_refuse_a_changed_file(tmp_path):
    path = tmp_path / "picks.json"
    digest = mh.freeze([{"id": "P01", "metros": ["35620"]}], path)
    assert len(digest) == 64 and mh.load_frozen(path)[0]["id"] == "P01"
    path.write_text(path.read_text().replace("35620", "33100"))
    with pytest.raises(ValueError, match="changed after it was frozen"):
        mh.load_frozen(path)


def test_holm_matches_the_step_down_by_hand():
    assert mh.holm([0.01, 0.04, 0.03, 0.2]) == pytest.approx([0.04, 0.09, 0.09, 0.2])


def test_positions_follow_the_frozen_sign_and_threshold():
    x = np.array([1.0, 2.0, 3.0])
    assert mh.positions(x, 1, 2.0).tolist() == [-1, -1, 1]
    assert mh.positions(x, -1, 2.0).tolist() == [1, -1, -1]
