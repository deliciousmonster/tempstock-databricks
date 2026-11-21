"""The backtest's explicit model: a ThresholdModel refit each year on labels known by then.

A session's label is WMT's simple return from close t+1 to close t+2: a 21:30 UTC decision on t
cannot trade t to t+1. The fit picks the sign and threshold whose trades earned the most of it over
every earlier session whose label was known at the refit; with no such pair it holds.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from weather_signal.config import PRIMARY
from weather_signal.signal.model import ThresholdModel

NAME, VERSION = "threshold_wf", "2"
GRID_C = (0.5, 1.0, 1.5, 2.0, 3.0)
MIN_TRADES = 30


@dataclass(frozen=True)
class Example:
    value: float
    ret: float
    known_at: datetime


@dataclass(frozen=True)
class Refit:
    """One refit of the walk: the model, the instant its labels were read at, and its period."""

    model: ThresholdModel
    fit_as_of: datetime
    n_train: int
    period_through: date


def labels(prices: pd.DataFrame) -> dict[date, tuple[float, datetime]]:
    """Per primary session t, the return from close t+1 to close t+2 and when close t+2 was known."""
    px = prices[prices["ticker"] == PRIMARY].sort_values("trade_date")
    days = [pd.Timestamp(d).date() for d in px["trade_date"]]
    close, known = px["close"].tolist(), px["known_at"].tolist()
    return {
        days[i]: (close[i + 2] / close[i + 1] - 1, pd.Timestamp(known[i + 2]).to_pydatetime())
        for i in range(len(days) - 2)
    }


def _pnl(examples: list[Example], coef: float, t: float) -> tuple[float, int]:
    pos = [(1 if coef * e.value > t else -1 if coef * e.value < -t else 0, e.ret) for e in examples]
    return sum(p * r for p, r in pos), sum(1 for p, _ in pos if p)


def fit(examples: Iterable[Example], as_of: datetime) -> ThresholdModel:
    """Only examples whose label was known at `as_of` take part."""
    return _fit([e for e in examples if e.known_at <= as_of])


def refit(examples: Iterable[Example], as_of: datetime) -> Refit:
    """The fit at `as_of`, trading through 31 December of that year."""
    seen = [e for e in examples if e.known_at <= as_of]
    return Refit(_fit(seen), as_of, len(seen), date(as_of.year, 12, 31))


def _fit(seen: list[Example]) -> ThresholdModel:
    best, best_pnl = (0.0, GRID_C[0]), 0.0
    for coef in (1.0, -1.0):
        for t in GRID_C:
            pnl, n = _pnl(seen, coef, t)
            if n >= MIN_TRADES and pnl > best_pnl:
                best, best_pnl = (coef, t), pnl
    return ThresholdModel(*best, model_name=NAME, model_version=VERSION)
