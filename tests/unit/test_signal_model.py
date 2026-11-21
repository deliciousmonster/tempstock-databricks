"""Freshness holds: `stale_*` and `horizon_unavailable` give `hold`; `AlwaysHold` never trades."""

import math
from datetime import date

import pytest

from tests.conftest import utc
from weather_signal.signal.model import (
    FORECAST_FEATURE,
    PRICE_FEATURES,
    AlwaysHold,
    BaseSignalModel,
    SignalInput,
    ThresholdModel,
)

FULL = {FORECAST_FEATURE: 3.0, **{p: 0.01 for p in PRICE_FEATURES}}


def _x(features, k=1):
    return SignalInput(utc(2026, 12, 2, 12, 30), date(2026, 12, 3), k, features, utc(2026, 12, 2))


def test_fresh_inputs_trade():
    assert ThresholdModel(1.0, 1.0).predict(_x(FULL)).action == "buy"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({FORECAST_FEATURE: math.nan}, "stale_forecast"),
        ({"spy_ret_5d": math.nan}, "stale_prices"),
        ({"stock_ret_1d": math.nan}, "stale_prices"),
    ],
)
def test_stale_holds(change, reason):
    s = ThresholdModel(1.0, 1.0).predict(_x({**FULL, **change}))
    assert s.action == "hold" and reason in s.reasons


def test_missing_feature_is_stale_unknown():
    feats = dict(FULL)
    del feats["xlp_ret_5d"]
    s = ThresholdModel(1.0, 1.0).predict(_x(feats))
    assert s.action == "hold" and s.reasons == ("stale_unknown",)


def test_horizon_6_unavailable():
    s = ThresholdModel(1.0, 1.0).predict(_x(FULL, 6))
    assert s.action == "hold" and s.reasons == ("horizon_unavailable",)


def test_always_hold_never_trades():
    for v in (-100.0, 0.0, 100.0):
        assert AlwaysHold().predict(_x({**FULL, FORECAST_FEATURE: v})).action == "hold"


def test_base_class_says_previous_session():
    assert "previous session" in (BaseSignalModel.__doc__ or "")


def test_a_missing_normal_is_not_a_stale_forecast():
    feats = {**FULL, FORECAST_FEATURE: math.nan, "forecast_no_normal": 1.0}
    s = ThresholdModel(1.0, 1.0).predict(_x(feats))
    assert s.action == "hold" and s.reasons == ("no_normal",)


def test_a_quarantined_forecast_holds_under_its_own_reason():
    feats = {**FULL, FORECAST_FEATURE: math.nan, "forecast_quarantined": 1.0}
    s = ThresholdModel(1.0, 1.0).predict(_x(feats))
    assert s.action == "hold" and s.reasons == ("forecast_quarantined",)
