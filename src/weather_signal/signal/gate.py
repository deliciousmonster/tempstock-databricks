"""The champion gate: the walk's net P&L against year-block shuffles of its own positions.

A position is +1 (buy), -1 (sell) or 0, held on WMT's raw simple return from close t+1 to close t+2.
Net daily P&L is `pos * ret - COST * [pos != 0]`. The gate shuffles the order of each calendar
year's block of positions `SHUFFLES` times from `random.Random(SEED)`, each shuffle paying the same
costs, and passes only when the real net total is above the shuffles' 95th percentile and above 0.
The draws and the percentile follow the reference's `pattern_stats`, so equal inputs give equal numbers.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

COST = 0.001
SHUFFLES = 200
SEED = 0
PERCENTILE = 95.0
POSITION = {"buy": 1, "sell": -1}
SIDES = tuple(POSITION)


@dataclass(frozen=True)
class Day:
    session: date
    pos: int
    ret: float


@dataclass(frozen=True)
class Gate:
    total: float
    p95: float | None
    passed: bool
    n: int
    trades: int


@dataclass(frozen=True)
class Side:
    """The walk's days on one side: their mean gross return and how many there were."""

    mean_gross: float | None
    n: int


def position(action: str) -> int:
    return POSITION.get(action, 0)


def pnl(pos: int, ret: float, cost: float = COST) -> float:
    return pos * ret - (cost if pos else 0.0)


def percentile(values: Sequence[float], q: float) -> float:
    """Linear interpolation between the closest ranks; NaN for no values."""
    v = sorted(x for x in values if not math.isnan(x))
    if not v:
        return math.nan
    at = (len(v) - 1) * q / 100
    lo = math.floor(at)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (at - lo)


def shuffle_totals(
    days: Sequence[Day], *, shuffles: int = SHUFFLES, seed: int = SEED, cost: float = COST
) -> list[float]:
    """Net totals of `shuffles` permutations of the yearly position blocks over the same returns."""
    rng = random.Random(seed)
    years = sorted({d.session.year for d in days})
    blocks = [[d.pos for d in days if d.session.year == y] for y in years]
    rets = [d.ret for d in days]
    totals = []
    for _ in range(shuffles):
        order = list(range(len(blocks)))
        rng.shuffle(order)
        pos = [p for j in order for p in blocks[j]]
        totals.append(sum(pnl(p, r, cost) for p, r in zip(pos, rets, strict=True)))
    return totals


def judge(
    days: Sequence[Day], *, shuffles: int = SHUFFLES, seed: int = SEED, cost: float = COST
) -> Gate:
    """`days` in session order, each with a known return."""
    total = sum(pnl(d.pos, d.ret, cost) for d in days)
    bar = percentile(shuffle_totals(days, shuffles=shuffles, seed=seed, cost=cost), PERCENTILE)
    passed = bool(days) and not math.isnan(bar) and total > bar and total > 0
    trades = sum(1 for d in days if d.pos)
    return Gate(total, None if math.isnan(bar) else bar, passed, len(days), trades)


def sides(days: Sequence[Day]) -> dict[str, Side]:
    """Per side, the mean gross return over the days the walk held it, to 8 dp as the reference's."""
    out = {}
    for side, pos in POSITION.items():
        rets = [d.ret for d in days if d.pos == pos]
        out[side] = Side(round(sum(rets) / len(rets), 8) if rets else None, len(rets))
    return out
