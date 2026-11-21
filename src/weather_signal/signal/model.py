"""The deterministic signal model contract, its freshness rule, and the two models this slice ships."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from weather_signal.config import BENCHMARK_FIRST, BENCHMARKS, PRIMARY, key

Action = Literal["buy", "sell", "hold"]
HORIZON_CAP = 6
FORECAST_FEATURE = "forecast_anomaly_c"
NO_NORMAL = "forecast_no_normal"
QUARANTINED = "forecast_quarantined"
STOCK_RET_1D, STOCK_RET_5D = f"{key(PRIMARY)}_ret_1d", f"{key(PRIMARY)}_ret_5d"
PRICE_FEATURES = (STOCK_RET_1D, STOCK_RET_5D, *(f"{key(b)}_ret_5d" for b in BENCHMARKS))
# A benchmark's return whose flag is set was not yet listed; missing, it does not hold the call.
UNLISTED = {f"{key(b)}_ret_5d": f"{key(b)}_unlisted" for b in BENCHMARKS if b in BENCHMARK_FIRST}


@dataclass(frozen=True)
class SignalInput:
    as_of: datetime
    target_date: date
    horizon_days: int
    features: Mapping[str, float]
    inputs_known_at: datetime


@dataclass(frozen=True)
class Signal:
    action: Action
    score: float
    confidence: float
    reasons: tuple[str, ...] = ()


def _missing(v: float | None) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def _unlisted(f: Mapping[str, float], price: str) -> bool:
    return price in UNLISTED and bool(f.get(UNLISTED[price]))


def _forecast_reason(f: Mapping[str, float]) -> str:
    if f.get(QUARANTINED):
        return "forecast_quarantined"
    return "no_normal" if f.get(NO_NORMAL) else "stale_forecast"


def freshness(x: SignalInput) -> tuple[str, ...]:
    """Why this input must hold: a capped horizon, or an input not visible at `as_of`."""
    if x.horizon_days >= HORIZON_CAP:
        return ("horizon_unavailable",)
    if FORECAST_FEATURE not in x.features or any(p not in x.features for p in PRICE_FEATURES):
        return ("stale_unknown",)
    reasons = []
    if _missing(x.features[FORECAST_FEATURE]):
        reasons.append(_forecast_reason(x.features))
    if any(_missing(x.features[p]) and not _unlisted(x.features, p) for p in PRICE_FEATURES):
        reasons.append("stale_prices")
    return tuple(reasons)


class BaseSignalModel:
    """Features: the forecast anomaly at lead k+2, and returns through the previous session.

    `predict` holds on any freshness reason before `decide` sees the input.
    """

    model_name = "base"
    model_version = "0"

    def predict(self, x: SignalInput) -> Signal:
        reasons = freshness(x)
        if reasons:
            return Signal("hold", 0.0, 0.0, reasons)
        return self.decide(x)

    def decide(self, x: SignalInput) -> Signal:
        raise NotImplementedError


class AlwaysHold(BaseSignalModel):
    model_name = "always_hold"
    model_version = "1"

    def decide(self, x: SignalInput) -> Signal:
        return Signal("hold", 0.0, 1.0)


@dataclass
class ThresholdModel(BaseSignalModel):
    """score = coef x forecast anomaly; trade past +-threshold, per horizon if fitted per horizon."""

    coef: float
    threshold: float
    model_name: str = "threshold"
    model_version: str = "1"

    def decide(self, x: SignalInput) -> Signal:
        s = self.coef * float(x.features[FORECAST_FEATURE])
        conf = min(1.0, abs(s) / (2 * self.threshold)) if self.threshold > 0 else 1.0
        if s > self.threshold:
            return Signal("buy", s, conf)
        if s < -self.threshold:
            return Signal("sell", s, conf)
        return Signal("hold", s, conf)


def from_params(params: Mapping[str, object], name: str, version: str) -> BaseSignalModel:
    if params.get("kind") == "threshold":
        return ThresholdModel(
            float(str(params["coef"])),
            float(str(params["threshold"])),
            model_name=name,
            model_version=version,
        )
    m = AlwaysHold()
    m.model_name, m.model_version = name, version
    return m
