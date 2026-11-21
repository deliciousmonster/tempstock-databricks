"""The thesis statistics: residual returns, lagged cross-correlation and Granger tests with Bonferroni."""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats


def returns(closes: pd.Series) -> pd.Series:
    return closes.pct_change().dropna()


def residualize(y: pd.Series, x: pd.DataFrame, window: int = 252) -> pd.Series:
    """Residual of y on x at each t from an OLS fitted on the `window` points before t only."""
    out = pd.Series(np.nan, index=y.index)
    xv, yv = np.column_stack([np.ones(len(x)), x.to_numpy()]), y.to_numpy()
    for i in range(window, len(y)):
        beta, *_ = np.linalg.lstsq(xv[i - window : i], yv[i - window : i], rcond=None)
        out.iloc[i] = yv[i] - xv[i] @ beta
    return out.dropna()


def lagged_xcorr(
    x: pd.Series, y: pd.Series, max_lag: int = 10, n_boot: int = 200, block: int = 20, seed: int = 0
) -> pd.DataFrame:
    """corr(x_t, y_{t+lag}) per lag, with a 95% band from a moving-block bootstrap of y."""
    rng = np.random.default_rng(seed)
    xv, yv = x.to_numpy(), y.to_numpy()
    n = len(yv)

    def corr(yy: np.ndarray, lag: int) -> float:
        return float(np.corrcoef(xv[: n - lag], yy[lag:])[0, 1])

    rows = []
    for lag in range(max_lag + 1):
        boots = []
        for _ in range(n_boot):
            starts = rng.integers(0, n - block, size=n // block + 1)
            yy = np.concatenate([yv[s : s + block] for s in starts])[:n]
            boots.append(corr(yy, lag))
        lo, hi = np.percentile(boots, [2.5, 97.5])
        rows.append({"lag": lag, "corr": corr(yv, lag), "band_lo": lo, "band_hi": hi})
    return pd.DataFrame(rows)


def _ols_rss(y: np.ndarray, x: np.ndarray) -> float:
    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    r = y - x @ beta
    return float(r @ r)


def granger(x: pd.Series, y: pd.Series, maxlag: int = 5) -> dict[int, float]:
    """p-value per lag of the F-test that x's lags add to y's own lags."""
    xv, yv = x.to_numpy(float), y.to_numpy(float)
    out = {}
    for p in range(1, maxlag + 1):
        n = len(yv) - p
        target = yv[p:]
        own = np.column_stack([np.ones(n)] + [yv[p - i : -i] for i in range(1, p + 1)])
        full = np.column_stack([own] + [xv[p - i : -i] for i in range(1, p + 1)])
        rss_r, rss_u = _ols_rss(target, own), _ols_rss(target, full)
        df_num, df_den = p, n - full.shape[1]
        f = ((rss_r - rss_u) / df_num) / (rss_u / df_den)
        out[p] = float(stats.f.sf(f, df_num, df_den))
    return out


def granger_significant(x: pd.Series, y: pd.Series, maxlag: int = 5, alpha: float = 0.05) -> bool:
    """Any lag significant after Bonferroni over the `maxlag` tests."""
    p = granger(x, y, maxlag)
    return min(p.values()) * len(p) < alpha
