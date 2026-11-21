"""The measured result of a backtest run, from the run's own rows: each buy or sell against the
next session's close, beside always buying and the same buys and sells on random days.

The random baseline is the exact mean and variance of drawing the run's buy and sell counts
without replacement from its days, read through a normal approximation.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from statistics import NormalDist
from typing import Any

Z95 = NormalDist().inv_cdf(0.975)


Move = tuple[str, str, float]
# Input kinds reported apart: a forecast is the thesis; an observed ERA5 anomaly stands in for one.
SLICES = ("forecast", "observed")


def _moves(rows: Iterable[Mapping[str, Any]]) -> list[Move]:
    """(action, input kind, move to the next close) for each session whose next close is known."""
    rs = sorted(
        (r for r in rows if r.get("target_date") and r.get("action")),
        key=lambda r: str(r["target_date"]),
    )
    out = []
    for r, nxt in zip(rs, rs[1:], strict=False):
        c0, c1 = r.get("close"), nxt.get("close")
        if isinstance(c0, int | float) and isinstance(c1, int | float) and c0 > 0:
            out.append((str(r["action"]), str(r.get("input_kind")), c1 / c0 - 1))
    return out


def random_hits(n_days: int, ups: int, downs: int, buys: int, sells: int) -> tuple[float, float]:
    """Mean and sd of hits when `buys` and `sells` land on distinct random days."""
    n, pairs = n_days, n_days * (n_days - 1)
    mean = buys * ups / n + sells * downs / n
    second = (
        mean
        + buys * (buys - 1) / pairs * (ups * ups - ups)
        + sells * (sells - 1) / pairs * (downs * downs - downs)
        + 2 * buys * sells / pairs * ups * downs
    )
    return mean, math.sqrt(max(second - mean * mean, 0.0))


def _stats(moves: list[Move]) -> dict[str, Any] | None:
    """Hit rate and baselines over `moves`, or None with fewer than two days or no trade."""
    trades = [(a, m) for a, _, m in moves if a in ("buy", "sell")]
    if len(moves) < 2 or not trades:
        return None
    ups = sum(m > 0 for _, _, m in moves)
    downs = sum(m < 0 for _, _, m in moves)
    buys = sum(a == "buy" for a, _ in trades)
    n = len(trades)
    hits = sum((a == "buy" and m > 0) or (a == "sell" and m < 0) for a, m in trades)
    mean, sd = random_hits(len(moves), ups, downs, buys, n - buys)
    at_or_above = 1 - NormalDist(mean, sd).cdf(hits - 0.5) if sd > 0 else float(mean >= hits)
    return {
        "n_days": len(moves),
        "n_trades": n,
        "buys": buys,
        "sells": n - buys,
        "hit_rate": round(hits / n, 4),
        "always_buy": round(ups / len(moves), 4),
        "random_mean": round(mean / n, 4),
        "random_band": [round((mean - Z95 * sd) / n, 4), round((mean + Z95 * sd) / n, 4)],
        "share_random_at_or_above": round(at_or_above, 4),
    }


def summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The run's result, and each input kind's apart; None when the run has no measurable trade."""
    moves = _moves(rows)
    whole = _stats(moves)
    if whole is None:
        return None
    dates = sorted(str(r["target_date"]) for r in rows if r.get("target_date"))
    by_input = {k: _stats([m for m in moves if m[1] == k]) for k in SLICES}
    # A slice too small for `_stats` still has its trades; the caption must not call them 0.
    trades_by_input = {k: sum(a in ("buy", "sell") and i == k for a, i, _ in moves) for k in SLICES}
    return {
        "backtest_run_id": rows[0].get("backtest_run_id"),
        "first": dates[0],
        "last": dates[-1],
        **whole,
        "by_input": by_input,
        "trades_by_input": trades_by_input,
    }
