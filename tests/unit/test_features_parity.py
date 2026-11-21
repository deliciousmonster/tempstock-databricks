"""Features are a function of decision day and target, seen at `as_of`.

The plan quotes all three tickers visible from 02:30 after a summer half day at a 7.5 h lag. SPY
lands at 02:05 and XLP at 01:05 as `ledger6.py` gives, but the WMT bar lands at 03:05 here.
"""

from datetime import date, timedelta

import pytest

from tests.conftest import utc
from tests.unit import tables
from weather_signal import asof
from weather_signal.signal import score
from weather_signal.signal.features import Tables, build
from weather_signal.signal.model import AlwaysHold, SignalInput, freshness

D = date(2026, 12, 2)  # a Wednesday


def _tabs(cal, p):
    return Tables(
        tables.forecast(D - timedelta(days=10), 30, p),
        tables.prices_at_close(cal, D - timedelta(days=30), D + timedelta(days=5)),
    )


def _feats(cal, tabs, as_of, p):
    d = asof.decision_day(as_of, p)
    return {
        k: build(d, asof.target_date(d, k), tabs, as_of=as_of, cal=cal)
        for k in asof.horizons(as_of, p)
        if k < 6
    }


def test_live_and_backtest_identical_at_2130(cal):
    tabs = _tabs(cal, 30)
    live = asof.live_as_of(utc(2026, 12, 2, 21, 5))
    assert live == asof.backtest_as_of(D)
    a, b = _feats(cal, tabs, live, 30), _feats(cal, tabs, asof.backtest_as_of(D), 30)
    assert {k: f.values for k, f in a.items()} == {k: f.values for k, f in b.items()}


def test_lead_column_is_k_plus_2_at_2330_p24(cal):
    tabs = _tabs(cal, 24)
    as_of = utc(2026, 12, 2, 23, 30)
    for k, f in _feats(cal, tabs, as_of, 24).items():
        target = asof.target_date(D, k)
        assert f.lead_days == k + 2
        assert f.values["forecast_anomaly_c"] == pytest.approx(
            (target - (D - timedelta(days=10))).days * 0.01 + k + 2
        )
        rows = tabs.forecast
        k1 = rows[(rows["target_date"] == target) & (rows["lead_days"] == k + 1)]
        assert (k1["known_at"] <= as_of).all()  # k+1 is usable, and not used


def test_0130_equals_2130_of_previous_decision_day_at_p28(cal):
    tabs = _tabs(cal, 28)
    late = _feats(cal, tabs, utc(2026, 12, 3, 1, 30), 28)
    early = _feats(cal, tabs, utc(2026, 12, 2, 21, 30), 28)
    assert set(late) == {1, 2, 3, 4, 5}
    for k in late:
        assert late[k].values == early[k].values and late[k].lead_days == early[k].lead_days


def _price_ok(f):
    return not any(
        r == "stale_prices"
        for r in freshness(
            SignalInput(utc(2026, 1, 1), date(2026, 1, 1), 1, f.values, utc(2026, 1, 1))
        )
    )


def _ledger_prices(cal, day, lag):
    start = utc(day.year, day.month, day.day) - timedelta(days=12)
    return tables.prices_from_ledger(cal, start, 24 * 15, lag)


@pytest.mark.parametrize("day", [date(2026, 12, 2), date(2026, 11, 30), date(2026, 7, 15)])
@pytest.mark.parametrize("p", [25, 30])
@pytest.mark.parametrize("hour", [0, 12])
def test_previous_session_visible_with_1h_lag(cal, day, p, hour):
    tabs = Tables(tables.forecast(day - timedelta(days=10), 30, p), _ledger_prices(cal, day, 1.0))
    as_of = utc(day.year, day.month, day.day, hour, 30)
    d = asof.decision_day(as_of, p)
    f = build(d, d + timedelta(days=1), tabs, as_of=as_of, cal=cal)
    assert _price_ok(f), (as_of, f.values)


def test_after_summer_half_day_at_7_5h_lag(cal):
    half = date(2025, 7, 3)
    px = tables.prices_from_ledger(cal, utc(2025, 6, 20), 24 * 16, 7.5)
    first = {
        t: px[(px["ticker"] == t) & (px["trade_date"] == half)]["known_at"].min()
        for t in ("SPY", "XLP", "WMT")
    }
    assert first["SPY"] == utc(2025, 7, 4, 2, 5, 30)
    assert first["XLP"] == utc(2025, 7, 4, 1, 5, 30)
    assert first["WMT"] == utc(2025, 7, 4, 3, 5, 30)
    tabs = Tables(tables.forecast(half - timedelta(days=10), 30, 30), px)
    seen = [
        h
        for h in range(24)
        if _price_ok(
            build(date(2025, 7, 4), date(2025, 7, 5), tabs, as_of=utc(2025, 7, 4, h, 30), cal=cal)
        )
    ]
    assert seen == list(range(3, 24))


def test_2_5h_lag_holds_live_while_backtest_sees_the_bar(cal):
    tabs = Tables(tables.forecast(D - timedelta(days=10), 30, 25), _ledger_prices(cal, D, 2.5))
    for h, ok in ((0, False), (1, False), (2, True)):
        f = build(D, D + timedelta(days=1), tabs, as_of=utc(2026, 12, 2, h, 30), cal=cal)
        assert _price_ok(f) is ok, h
    f = build(D, D + timedelta(days=1), tabs, as_of=asof.backtest_as_of(D), cal=cal)
    assert _price_ok(f)


def test_horizon_6_is_unavailable(cal):
    rows = score.frame(utc(2026, 12, 2, 12, 30), 30, _tabs(cal, 30), cal, AlwaysHold())
    six = [r for r in rows if r["horizon_days"] == 6]
    assert six and six[0]["action"] == "hold" and six[0]["reasons"] == ["horizon_unavailable"]


def _valueless_reasons(cal, **cols):
    """The freshness reasons at every horizon when every visible cell has no value."""
    tabs = _tabs(cal, 30)
    tabs.forecast["anomaly_c"] = float("nan")
    for c, v in cols.items():
        tabs.forecast[c] = v
    as_of = asof.live_as_of(utc(2026, 12, 2, 21, 5))
    for k, f in _feats(cal, tabs, as_of, 30).items():
        target = asof.target_date(D, k)
        yield freshness(SignalInput(as_of, target, k, f.values, as_of))


def test_a_visible_forecast_without_a_normal_holds_as_no_normal(cal):
    for reasons in _valueless_reasons(cal):
        assert "no_normal" in reasons and "stale_forecast" not in reasons


def test_a_visible_quarantined_cell_holds_as_forecast_quarantined(cal):
    for reasons in _valueless_reasons(cal, quarantined=True):
        assert "forecast_quarantined" in reasons
        assert "stale_forecast" not in reasons and "no_normal" not in reasons
