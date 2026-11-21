"""Frozen picks and the holdout tests they face: hit rate, a shift p-value, Holm, a bootstrap CI."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from weather_signal.research.metro_search import circular_shift, shift_offsets


def freeze(picks: list[dict[str, Any]], path: Path) -> str:
    """Write the picks as canonical JSON and its sha256 beside it; return the hash."""
    body = json.dumps(picks, sort_keys=True, indent=1).encode()
    digest = hashlib.sha256(body).hexdigest()
    path.write_bytes(body)
    path.with_suffix(".sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return digest


def load_frozen(path: Path) -> list[dict[str, Any]]:
    """The picks, refused if the file no longer matches the hash written when they were frozen."""
    body = path.read_bytes()
    want = path.with_suffix(".sha256").read_text(encoding="utf-8").split()[0]
    if hashlib.sha256(body).hexdigest() != want:
        raise ValueError(f"{path} changed after it was frozen")
    picks: list[dict[str, Any]] = json.loads(body)
    return picks


def positions(x: np.ndarray, sign: int, threshold: float) -> np.ndarray:
    """+1 (buy) where sign * (x - threshold) > 0, else -1 (sell)."""
    return np.where(sign * (x - threshold) > 0, 1, -1)


def hit_rate(pos: np.ndarray, y: np.ndarray) -> float:
    return float((np.sign(y) == pos).mean())


def random_band(
    y: np.ndarray, buy_share: float, n: int = 1000, seed: int = 0
) -> tuple[float, float, float]:
    """Mean and 2.5/97.5 percentiles of hit rates from random calls buying at `buy_share`."""
    rng = np.random.default_rng(seed)
    rates = [hit_rate(np.where(rng.random(len(y)) < buy_share, 1, -1), y) for _ in range(n)]
    lo, hi = np.percentile(rates, [2.5, 97.5])
    return float(np.mean(rates)), float(lo), float(hi)


def corr(x: np.ndarray, y: np.ndarray) -> float:
    return float(np.corrcoef(x, y)[0, 1])


def block_bootstrap_ci(
    x: np.ndarray, y: np.ndarray, block: int = 20, n: int = 2000, seed: int = 0
) -> tuple[float, float]:
    """95% interval of corr(x, y) from a moving-block bootstrap of the paired rows."""
    rng = np.random.default_rng(seed)
    m = len(x)
    out = []
    for _ in range(n):
        starts = rng.integers(0, m - block + 1, size=m // block + 1)
        rows = np.concatenate([np.arange(s, s + block) for s in starts])[:m]
        out.append(corr(x[rows], y[rows]))
    lo, hi = np.percentile(out, [2.5, 97.5])
    return float(lo), float(hi)


def shift_pvalue(x: np.ndarray, y: np.ndarray, sign: int, n: int = 2000, seed: int = 0) -> float:
    """One-sided p of sign * corr(x, y) against circular shifts of y of 30 rows or more."""
    real = sign * corr(x, y)
    offs = shift_offsets(len(y), n, np.random.default_rng(seed))
    null = np.array([sign * corr(x, circular_shift(y, int(o))) for o in offs])
    return float((1 + (null >= real).sum()) / (1 + n))


def holm(pvalues: list[float]) -> list[float]:
    """Holm step-down adjusted p-values, in the input order."""
    m = len(pvalues)
    order = np.argsort(pvalues)
    adj = np.empty(m)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[i]))
        adj[i] = running
    return [float(a) for a in adj]
