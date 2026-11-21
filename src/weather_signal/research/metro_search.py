"""The in-sample search over metros and the circular-shift null it is judged against."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

import numpy as np

from weather_signal.research.metro_data import KINDS, LAGS, Panel

MIN_SHIFT = 30


def circular_shift(y: np.ndarray, offset: int) -> np.ndarray:
    """Rows of y rotated by `offset`; each series keeps its own autocorrelation."""
    return np.roll(y, offset, axis=0)


def shift_offsets(
    n: int, k: int, rng: np.random.Generator, min_shift: int = MIN_SHIFT
) -> np.ndarray:
    """k offsets in [min_shift, n - min_shift], so no placebo sits within min_shift rows of the truth."""
    if n < 2 * min_shift + 1:
        raise ValueError(f"{n} rows leave no offset at least {min_shift} from zero")
    return rng.integers(min_shift, n - min_shift + 1, size=k)


class SubsetScorer:
    """Pearson correlation of each subset's equal-weight mean column of X with each column of Y."""

    def __init__(self, x: np.ndarray, idx: np.ndarray):
        self.xc = x - x.mean(axis=0)
        self.idx = idx
        g = self.xc.T @ self.xc
        self.sd = np.sqrt(g[idx[:, :, None], idx[:, None, :]].sum(axis=(1, 2)))

    def corr(self, y: np.ndarray) -> np.ndarray:
        yc = y - y.mean(axis=0)
        cov = (self.xc.T @ yc)[self.idx].sum(axis=1)
        return cov / (self.sd[:, None] * np.sqrt((yc * yc).sum(axis=0))[None, :])


@dataclass
class Scores:
    screen: np.ndarray  # (kind, lag) x metro x target
    combos: dict[int, np.ndarray]  # size -> lag x subset x target, level anomalies only

    def maxima(self) -> dict[str, float]:
        out = {"screen": float(np.abs(self.screen).max())}
        for s, v in self.combos.items():
            out[f"size{s}"] = float(np.abs(v).max())
        out["combo"] = max(out[f"size{s}"] for s in self.combos)
        return out

    def per_metro(self) -> np.ndarray:
        return np.abs(self.screen).max(axis=(0, 2))


class Search:
    """Every metro at every lag and kind, and every pair and triple, against every target."""

    def __init__(self, p: Panel, sizes: tuple[int, ...] = (2, 3)):
        m = len(p.metros)
        self.keys = [(k, lag) for k in KINDS for lag in LAGS]
        one = np.arange(m)[:, None]
        self.single = {key: SubsetScorer(p.features[key], one) for key in self.keys}
        self.idx = {s: np.array(list(combinations(range(m), s))) for s in sizes}
        self.combo = {
            (lag, s): SubsetScorer(p.features[("level", lag)], self.idx[s])
            for lag in LAGS
            for s in sizes
        }

    def run(self, y: np.ndarray) -> Scores:
        screen = np.stack([self.single[key].corr(y) for key in self.keys])
        combos = {s: np.stack([self.combo[(lag, s)].corr(y) for lag in LAGS]) for s in self.idx}
        return Scores(screen, combos)


@dataclass
class Null:
    offsets: np.ndarray
    maxima: dict[str, np.ndarray] = field(default_factory=dict)
    per_metro: np.ndarray | None = None  # run x metro


def null_search(search: Search, y: np.ndarray, n: int, seed: int = 0) -> Null:
    """Rerun the whole search on `n` circular shifts of the targets, all targets shifted together."""
    offs = shift_offsets(len(y), n, np.random.default_rng(seed))
    runs = [search.run(circular_shift(y, int(o))) for o in offs]
    keys = runs[0].maxima().keys()
    return Null(
        offs,
        {k: np.array([r.maxima()[k] for r in runs]) for k in keys},
        np.stack([r.per_metro() for r in runs]),
    )


def null_share(real: float, null: np.ndarray) -> float:
    """Share of placebo runs scoring at or above the real one, counting the real run as one."""
    return float((1 + (null >= real).sum()) / (1 + len(null)))


def _soft(z: float, t: float) -> float:
    return float(np.sign(z) * max(abs(z) - t, 0.0))


def lasso_cd(
    g: np.ndarray, c: np.ndarray, alpha: float, b0: np.ndarray | None = None, tol: float = 1e-6
) -> np.ndarray:
    """Minimise b'Gb/2 - c'b + alpha|b|_1 (G = X'X/n, c = X'y/n) by coordinate descent."""
    b = np.zeros(len(c)) if b0 is None else b0.copy()
    gb, diag = g @ b, np.diag(g).tolist()
    for _ in range(1000):
        delta = 0.0
        for j in range(len(c)):
            new = _soft(c[j] - gb[j] + diag[j] * b[j], alpha) / diag[j]
            if new != b[j]:
                gb += g[:, j] * (new - b[j])
                delta = max(delta, abs(new - b[j]))
                b[j] = new
        if delta < tol:
            break
    return b


class LassoCV:
    """LASSO over the columns of x, its penalty chosen by contiguous-block cross-validation."""

    def __init__(self, x: np.ndarray, folds: int = 5, n_alpha: int = 12):
        n = len(x)
        self.n_alpha = n_alpha
        self.mu, self.sd = x.mean(axis=0), x.std(axis=0)
        self.z = (x - self.mu) / self.sd
        self.g = self.z.T @ self.z / n
        edges = np.linspace(0, n, folds + 1).astype(int)
        self.folds = []
        for lo, hi in zip(edges[:-1], edges[1:], strict=True):
            tr = np.r_[0:lo, hi:n]
            mu, sd = x[tr].mean(axis=0), x[tr].std(axis=0)
            ztr, zte = (x[tr] - mu) / sd, (x[lo:hi] - mu) / sd
            self.folds.append((tr, slice(lo, hi), ztr, zte, ztr.T @ ztr / len(tr)))

    def alphas(self, y: np.ndarray) -> np.ndarray:
        top = float(np.abs(self.z.T @ (y - y.mean())).max() / len(y))
        return top * np.logspace(0, -2, self.n_alpha)

    def cv_r2(self, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Out-of-fold R2 per penalty, pooled over folds, and the penalties tried (y in sd units)."""
        y = y / y.std()
        alphas = self.alphas(y)
        pred = np.zeros((len(y), len(alphas)))
        for tr, te, ztr, zte, g in self.folds:
            ym = y[tr].mean()
            c = ztr.T @ (y[tr] - ym) / len(tr)
            b = None
            for i, a in enumerate(alphas):
                b = lasso_cd(g, c, a, b)
                pred[te, i] = zte @ b + ym
        sse = ((y[:, None] - pred) ** 2).sum(axis=0)
        return 1 - sse / ((y - y.mean()) ** 2).sum(), alphas

    def fit(self, y: np.ndarray, alpha: float) -> np.ndarray:
        """Coefficients on the standardised columns, fitted on every row; alpha as cv_r2 gave it."""
        sd = y.std()
        return sd * lasso_cd(self.g, self.z.T @ (y - y.mean()) / (sd * len(y)), alpha)
