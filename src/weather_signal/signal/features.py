"""Features as a function of the decision day and the target, visible at `as_of`."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

from weather_signal import asof
from weather_signal.config import BENCHMARK_FIRST, PRIMARY, TICKERS, key
from weather_signal.sessions import Calendar
from weather_signal.signal.model import FORECAST_FEATURE, NO_NORMAL, QUARANTINED

NAN = math.nan


@dataclass(frozen=True)
class Tables:
    forecast: pd.DataFrame
    prices: pd.DataFrame


@dataclass
class Features:
    values: dict[str, float]
    lead_days: int
    known_at: list[datetime] = field(default_factory=list)
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def inputs_known_at(self) -> datetime | None:
        return max(self.known_at) if self.known_at else None


def _closes(prices: pd.DataFrame, ticker: str, as_of: datetime) -> dict[date, float]:
    rows = prices[prices["ticker"] == ticker]
    vis = asof.visible(rows, as_of, ["ticker", "trade_date"])
    return {
        pd.Timestamp(d).date(): float(c)
        for d, c in zip(vis["trade_date"], vis["close"], strict=True)
    }


def _ret(closes: dict[date, float], end: date, start: date) -> float:
    if end in closes and start in closes:
        return closes[end] / closes[start] - 1
    return NAN


def _why_no_value(vis: pd.DataFrame) -> str:
    """A visible cell without a value was quarantined, or its day has no normal."""
    return QUARANTINED if "quarantined" in vis and bool(vis["quarantined"].iloc[0]) else NO_NORMAL


def build(
    decision_day: date, target: date, tables: Tables, *, as_of: datetime, cal: Calendar
) -> Features:
    horizon = (target - decision_day).days
    lead = asof.fitted_lead(horizon)
    out = Features({}, lead)
    fc = tables.forecast
    rows = fc[(pd.to_datetime(fc["target_date"]).dt.date == target) & (fc["lead_days"] == lead)]
    vis = asof.visible(rows, as_of, ["target_date", "lead_days"])
    if len(vis):
        out.values[FORECAST_FEATURE] = float(vis["anomaly_c"].iloc[0])
        if math.isnan(out.values[FORECAST_FEATURE]):
            out.values[_why_no_value(vis)] = 1.0
        out.known_at.append(pd.Timestamp(vis["known_at"].iloc[0]).to_pydatetime())
        out.extra["forecast_known_at"] = out.known_at[-1].isoformat()
    else:
        out.values[FORECAST_FEATURE] = NAN
    prev = cal.previous(decision_day)
    prev1, prev5 = cal.previous(prev), prev
    for _ in range(5):
        prev5 = cal.previous(prev5)
    for t in TICKERS:
        closes = _closes(tables.prices, t, as_of)
        k = key(t)
        if t == PRIMARY:
            out.values[f"{k}_ret_1d"] = _ret(closes, prev, prev1)
            if prev in closes:
                out.extra |= {"last_close": closes[prev], "last_close_date": prev.isoformat()}
        out.values[f"{k}_ret_5d"] = _ret(closes, prev, prev5)
        if t in BENCHMARK_FIRST:
            # Before its sixth session a benchmark has no five-day return to be stale.
            out.values[f"{k}_unlisted"] = 1.0 if prev5 < BENCHMARK_FIRST[t] else 0.0
        sub = tables.prices[
            (tables.prices["ticker"] == t)
            & (pd.to_datetime(tables.prices["trade_date"]).dt.date == prev)
        ]
        vis_p = asof.visible(sub, as_of, ["ticker", "trade_date"])
        if len(vis_p):
            out.known_at.append(pd.Timestamp(vis_p["known_at"].iloc[0]).to_pydatetime())
    return out
