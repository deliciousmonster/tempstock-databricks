"""The backtest scores each session with only what was known at its 21:30 UTC decision."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

from tests.conftest import utc
from tests.unit.tables import prices_at_close
from weather_signal import asof
from weather_signal.catalog.ddl import BACKTEST_COLUMNS
from weather_signal.signal import backtest, backtest_fit
from weather_signal.signal.model import AlwaysHold, ThresholdModel

DAY = date(2015, 3, 10)
AS_OF = asof.backtest_as_of(DAY)
NOW = utc(2026, 11, 18, 12)
MODEL = ThresholdModel(1.0, 0.5, model_name="m", model_version="7")


def _forecast(*cells: tuple[float, datetime]) -> pd.DataFrame:
    cols = ["target_date", "lead_days", "anomaly_c", "known_at", "quarantined"]
    return pd.DataFrame([(DAY, backtest.LEAD, a, k, False) for a, k in cells], columns=cols)


def _observed(*days: tuple[date, float]) -> pd.DataFrame:
    """Observed anomalies, each known eight days after its day as the ERA5 backfill is."""
    rows = [(d, a, asof.known_at_era5_backfill(d)) for d, a in days]
    return pd.DataFrame(rows, columns=["date", "anomaly_c", "known_at"])


def _inputs(cal, forecast: pd.DataFrame, observed: pd.DataFrame) -> backtest.Inputs:
    prices = prices_at_close(cal, DAY - timedelta(days=40), DAY + timedelta(days=10))
    return backtest.Inputs(forecast, prices, observed)


def _row(cal, forecast, observed, model=MODEL):
    return backtest.rows([DAY], _inputs(cal, forecast, observed), cal, model, NOW)[0]


def test_a_forecast_known_after_the_decision_is_not_used(cal):
    future = _forecast((3.0, AS_OF + timedelta(hours=1)))
    r = _row(cal, future, _observed((DAY - timedelta(days=8), -3.0)))
    assert (r["action"], r["input_kind"]) == ("sell", "observed")
    assert r["inputs_max_known_at"] <= AS_OF


def test_a_revision_known_after_the_decision_does_not_replace_the_forecast(cal):
    cells = _forecast((-3.0, AS_OF - timedelta(hours=1)), (3.0, AS_OF + timedelta(hours=1)))
    r = _row(cal, cells, _observed())
    assert (r["action"], r["input_kind"]) == ("sell", "forecast")


def test_an_observed_day_known_after_the_decision_is_not_used(cal):
    # t-7 lands at t+1 00:00, after the 21:30 decision on t; t-8, the latest known, at t 00:00.
    obs = _observed(
        (DAY - timedelta(days=10), 3.0),
        (DAY - timedelta(days=8), -3.0),
        (DAY - timedelta(days=7), 3.0),
    )
    r = _row(cal, _forecast(), obs)
    assert (r["action"], r["input_kind"]) == ("sell", "observed")
    assert '"observed_date": "2015-03-02"' in r["features"]


def test_nothing_known_holds_and_says_so(cal):
    r = _row(cal, _forecast(), _observed((DAY - timedelta(days=7), 3.0)))
    assert (r["action"], r["input_kind"], r["reasons"]) == ("hold", "none", ["no_input"])


def test_an_observed_day_without_a_normal_holds_and_names_it(cal):
    # Dec 31 of a leap year is day 366, which the normals leave null; the anomaly is NaN.
    r = _row(cal, _forecast(), _observed((DAY - timedelta(days=8), math.nan)))
    assert (r["action"], r["input_kind"], r["reasons"]) == ("hold", "observed", ["no_normal"])


def test_features_are_strict_json_so_sql_can_read_them(cal):
    r = _row(cal, _forecast(), _observed((DAY - timedelta(days=8), math.nan)))
    f = json.loads(r["features"], parse_constant=lambda c: pytest.fail(f"bare {c} in features"))
    assert f["forecast_anomaly_c"] is None and f["observed_date"] == "2015-03-02"


def test_the_summary_counts_each_kind_and_each_missing_input(cal):
    obs = _observed((DAY - timedelta(days=8), math.nan))
    rows = [
        _row(cal, _forecast((3.0, AS_OF - timedelta(hours=1))), _observed()),
        _row(cal, _forecast(), obs),
        _row(cal, _forecast(), _observed()),
    ]
    got = backtest.summary(rows)
    assert got == {
        "rows": 3,
        "forecast": 1,
        "observed": 1,
        "none": 1,
        "no_input": 1,
        "no_normal": 1,
        "stale_input": 0,
        "newest_stale": 0,
    }
    with pytest.raises(backtest.TooManyMissing, match="2 of 3"):
        backtest.check_missing(got)
    backtest.check_missing(got | {"rows": 1000})


@pytest.mark.parametrize(
    ("age", "action", "reasons"), [(14, "sell", []), (15, "hold", ["stale_input"])]
)
def test_an_observed_day_trades_up_to_the_age_limit_and_holds_past_it(cal, age, action, reasons):
    """2026-11-20 dev: 1,369 WMT buys from 2008 to 2013 all read the 2007-12-27 observation."""
    assert asof.MAX_OBSERVED_AGE == timedelta(days=14)
    seen = DAY - timedelta(days=age)
    r = _row(cal, _forecast(), _observed((seen, -3.0)))
    assert (r["action"], r["input_kind"], r["reasons"]) == (action, "observed", reasons)
    f = json.loads(r["features"])
    assert (f["forecast_anomaly_c"], f["observed_date"]) == (-3.0, seen.isoformat())


@pytest.mark.parametrize(("age", "stale"), [(8, False), (14, False), (15, True), (2015, True)])
def test_the_rule_compares_whole_days_to_the_limit(age, stale):
    assert asof.observed_stale(DAY, DAY - timedelta(days=age)) is stale


def test_a_stale_observation_teaches_the_fit_nothing(cal):
    inp = _inputs(cal, _forecast(), _observed((DAY - timedelta(days=15), -3.0)))
    assert backtest.examples([DAY], inp, cal) == []
    fresh = _inputs(cal, _forecast(), _observed((DAY - timedelta(days=14), -3.0)))
    assert [e.value for e in backtest.examples([DAY], fresh, cal)] == [-3.0]


def test_a_past_stale_gap_is_counted_but_does_not_fail_the_run(cal):
    stale = _observed((DAY - timedelta(days=15), 3.0))
    rows = [_row(cal, _forecast(), stale), _row(cal, _forecast(), _observed())]
    got = backtest.summary(rows)
    assert (got["observed"], got["stale_input"], got["no_input"]) == (1, 1, 1)
    assert got["newest_stale"] == 0
    backtest.check_missing(got | {"rows": 100})


@pytest.mark.parametrize("age", [15, 400])
def test_a_dead_observed_feed_fails_the_run(cal, age):
    """ERA5 cut to one old row once passed as a run of holds; an empty table failed."""
    fresh = _row(cal, _forecast(), _observed((DAY - timedelta(days=8), 3.0)))
    dead = _row(cal, _forecast(), _observed((DAY - timedelta(days=age), 3.0)))
    got = backtest.summary([fresh] * 999 + [dead])
    assert (got["stale_input"], got["newest_stale"]) == (1, 1)
    with pytest.raises(backtest.TooManyMissing, match="newest session"):
        backtest.check_missing(got)


def test_a_visible_forecast_wins_over_the_observed(cal):
    r = _row(
        cal,
        _forecast((3.0, AS_OF - timedelta(hours=1))),
        _observed((DAY - timedelta(days=8), -3.0)),
    )
    assert (r["action"], r["input_kind"]) == ("buy", "forecast")


def test_start_is_the_seventh_session_of_1991(cal):
    assert cal.between(date(1991, 1, 1), date(1991, 1, 31))[6] == backtest.START
    assert backtest.START == date(1991, 1, 10)


def test_the_session_before_start_holds_for_stale_prices_and_start_does_not(cal):
    """START is the first session with a five-day return."""
    prices = prices_at_close(cal, date(1991, 1, 2), date(1991, 1, 20))
    days = [date(1991, 1, 9), backtest.START]
    obs = _observed((date(1990, 12, 30), 3.0), (date(1991, 1, 1), 3.0))
    inp = backtest.Inputs(_forecast().iloc[0:0], prices, obs)
    before, start = backtest.rows(days, inp, cal, MODEL, NOW)
    assert before["action"] == "hold" and "stale_prices" in before["reasons"]
    assert "stale_prices" not in start["reasons"] and start["action"] == "buy"


def test_sessions_start_at_start_and_stop_before_now(cal):
    prices = prices_at_close(cal, date(1990, 12, 1), date(1991, 1, 20))
    now = datetime.combine(date(1991, 1, 15), time(21), tzinfo=asof.UTC)
    assert backtest.sessions(prices, now) == [
        date(1991, 1, 10),
        date(1991, 1, 11),
        date(1991, 1, 14),
    ]


def test_sessions_and_labels_read_the_primary_only(cal):
    prices = prices_at_close(cal, date(1991, 1, 1), date(1991, 1, 20))
    other = prices[prices["ticker"] == "SPY"].head(1).assign(trade_date=date(1991, 1, 19))
    with_other = pd.concat([prices, other])
    now = datetime.combine(date(1991, 1, 25), time(21), tzinfo=asof.UTC)
    assert date(1991, 1, 19) not in backtest.sessions(with_other, now)
    wmt = prices[prices["ticker"] == "WMT"].sort_values("trade_date")
    ret, _ = backtest_fit.labels(with_other)[date(1991, 1, 10)]
    c = dict(zip(wmt["trade_date"], wmt["close"], strict=True))
    # Decided after the close of the 10th: entered at the 11th's close, out at the 14th's.
    assert ret == pytest.approx(c[date(1991, 1, 14)] / c[date(1991, 1, 11)] - 1)


def test_rows_carry_the_primary_ticker(cal):
    r = _row(cal, _forecast((3.0, AS_OF - timedelta(hours=1))), _observed())
    assert r["ticker"] == "WMT" and backtest.run_id(MODEL) == "h0:m:7"


def test_row_matches_the_table_columns_in_order(cal):
    r = _row(cal, _forecast(), _observed(), AlwaysHold())
    names = [
        re.match(r"\s*(\w+)", c).group(1) for c in BACKTEST_COLUMNS.split(",") if " " in c.strip()
    ]
    assert list(r) == names
    assert (r["backtest_run_id"], r["horizon_days"], r["target_date"]) == (
        "h0:always_hold:1",
        0,
        DAY,
    )


@pytest.mark.parametrize("hours", [0, -1])
def test_an_input_known_at_the_decision_is_used(cal, hours):
    r = _row(cal, _forecast((3.0, AS_OF + timedelta(hours=hours))), _observed())
    assert r["input_kind"] == "forecast"


def _walk_world(cal, last_2014_ret: float):
    """WMT gains 1 % a session through 2014 and loses 1 % in 2015; the anomaly is +2 every day."""
    days = cal.between(date(2014, 11, 3), date(2015, 1, 30))
    closes, c = {}, 100.0
    for d in cal.between(date(2014, 9, 1), date(2015, 2, 27)):
        closes[d] = c
        r = 0.01 if d.year == 2014 else -0.01
        c *= 1 + (last_2014_ret if d == date(2014, 12, 31) else r)
    prices = pd.DataFrame(
        [
            {
                "ticker": t,
                "trade_date": d,
                "close": v if t == "WMT" else 100.0,
                "known_at": cal.close(d) + timedelta(hours=1),
            }
            for d, v in closes.items()
            for t in ("WMT", "SPY", "XLP")
        ]
    )
    obs = _observed(*[(date(2014, 8, 1) + timedelta(days=i), 2.0) for i in range(200)])
    return days, backtest.Inputs(_forecast(), prices, obs)


def test_the_first_year_holds_and_the_next_trades_on_what_it_learned(cal):
    days, inp = _walk_world(cal, 0.01)
    out = backtest.rows(days, inp, cal, None, NOW)
    by_year = {y: {r["action"] for r in out if r["target_date"].year == y} for y in (2014, 2015)}
    assert by_year == {2014: {"hold"}, 2015: {"buy"}}
    assert {r["backtest_run_id"] for r in out} == {"h0:threshold_wf:2"}
    assert '"model_coef": 1.0' in out[-1]["features"]


def test_a_label_known_after_the_refit_does_not_steer_it(cal):
    # 2014-12-31's label is the close of 2015-01-02, known at 22:00, after that day's 21:30 refit;
    # counted, its -99 % would turn the fit to sell.
    days, inp = _walk_world(cal, -0.99)
    out = backtest.rows(days, inp, cal, None, NOW)
    assert {r["action"] for r in out if r["target_date"].year == 2015} == {"buy"}


def test_fit_takes_the_sign_and_threshold_that_earned_and_holds_below_min_trades():
    k = utc(2015, 1, 1)
    warm_falls = [backtest_fit.Example(2.0, -0.01, k)] * 30 + [
        backtest_fit.Example(0.8, 0.005, k)
    ] * 30
    m = backtest_fit.fit(warm_falls, k)
    assert (m.coef, m.threshold) == (-1.0, 1.0)
    few = backtest_fit.fit(warm_falls[:29], k)
    assert few.coef == 0.0


def test_rows_carry_position_return_and_net_pnl(cal):
    days, inp = _walk_world(cal, 0.01)
    out = backtest.rows(days, inp, cal, None, NOW)
    buy = next(r for r in out if r["action"] == "buy")
    hold = next(r for r in out if r["action"] == "hold")
    # 2015 falls 1 % a session: the buy pays the fall from t+1 to t+2 and the 0.001 cost.
    assert (buy["pos"], buy["ret"]) == (1, pytest.approx(-0.01))
    assert buy["pnl"] == pytest.approx(-0.011)
    assert (hold["pos"], hold["pnl"]) == (0, 0.0)


def test_every_row_of_a_run_carries_its_gate(cal):
    days, inp = _walk_world(cal, 0.01)
    w = backtest.walk(days, inp, cal, NOW)
    gates = {(r["gate_total"], r["gate_p95"], r["gate_pass"], r["gate_n"]) for r in w.rows}
    assert gates == {(w.verdict.total, w.verdict.p95, False, len(days))}
    assert w.verdict.total == pytest.approx(sum(r["pnl"] for r in w.rows))


def test_a_session_whose_second_next_close_is_missing_has_no_return(cal):
    prices = prices_at_close(cal, DAY - timedelta(days=40), DAY + timedelta(days=1))
    inp = backtest.Inputs(_forecast(), prices, _observed())
    (r,) = backtest.rows([DAY], inp, cal, MODEL, NOW)
    assert (r["ret"], r["pnl"], r["gate_n"]) == (None, None, 0)
