"""A live signal row with a NaN price keeps its forecast anomaly readable by the views (2026-11-19
dev: the Outlook read "no normal" on every stale_prices hold)."""

from __future__ import annotations

import json
import math
import re
from datetime import date, timedelta

import pandas as pd

from tests.conftest import utc
from tests.spark.test_ui_views import _base, _rows, world  # noqa: F401
from tests.unit.tables import prices_at_close
from weather_signal import asof
from weather_signal.catalog import setup
from weather_signal.config import PRIMARY, Names
from weather_signal.signal import score
from weather_signal.signal.features import Features, Tables, build
from weather_signal.signal.model import AlwaysHold, Signal, SignalInput

AS_OF = utc(2026, 11, 19, 6, 30)
N = Names("workspace", "dev_")


def _features() -> str:
    f = Features({"forecast_anomaly_c": 2.97, "stock_ret_1d": math.nan, "xlp_ret_5d": math.inf}, 2)
    sig = Signal("hold", 0.0, 0.0, ("stale_prices",))
    return score.row(AS_OF, date(2026, 11, 19), 0, sig, AlwaysHold(), f, AS_OF)["features"]


def test_the_scorer_writes_no_bare_nan():
    f = json.loads(_features(), parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    assert f["stock_ret_1d"] is None and f["xlp_ret_5d"] is None


def test_the_calls_view_reads_the_anomaly_of_a_stale_prices_hold(world):  # noqa: F811
    feats = _features().replace("'", "\\'")
    _base(
        world,
        "signals__signal",
        "SELECT TIMESTAMP'2026-11-19 06:30:00' as_of, DATE'2026-11-19' target_date, 0 horizon_days, "
        "'hold' action, 0D score, 0D confidence, array('stale_prices') reasons, 'm' model_name, "
        f"'1' model_version, '{feats}' features, CAST(NULL AS TIMESTAMP) inputs_max_known_at, "
        "'WMT' ticker, CAST(NULL AS DOUBLE) est_price, 'hold' est_reason, CAST(NULL AS STRING) est_basis",
    )
    [row] = _rows(world, "v_ui_calls")
    assert row["forecast_anomaly_c"] == 2.97


def _declared(fn: str) -> list[str]:
    """The column names `RETURNS TABLE (...)` declares, in order."""
    cols = re.search(r"RETURNS TABLE \((.*?)\) RETURN", fn, re.S).group(1)
    return [c.split()[0] for c in cols.split(", ")]


def test_agent_inputs_of_a_stale_prices_hold_carry_what_was_known_and_null_what_was_not(spark, cal):
    """The agent sees the visible forecast and last close, and a null return,
    beside the model's hold. Built by `features.build`, so every value is visible at `as_of`."""
    day = date(2015, 3, 10)
    as_of = asof.backtest_as_of(day)
    prev = cal.previous(day)
    lead = asof.fitted_lead(0)
    fc = pd.DataFrame(
        [(day, lead, 2.97, as_of - timedelta(hours=1), False)],
        columns=["target_date", "lead_days", "anomaly_c", "known_at", "quarantined"],
    )
    # Two sessions of closes: a one-day return and a last close, no five-day return.
    prices = prices_at_close(cal, cal.previous(prev), prev)
    f = build(day, day, Tables(fc, prices), as_of=as_of, cal=cal)
    sig = AlwaysHold().predict(SignalInput(as_of, day, 0, f.values, f.inputs_known_at or as_of))
    assert sig.reasons == ("stale_prices",)
    feats = score.row(as_of, day, 0, sig, AlwaysHold(), f, as_of)["features"]
    spark.createDataFrame(
        [(as_of, day, 0, "hold", 0.0, 0.0, list(sig.reasons), "m", "1", feats, None)],
        "as_of TIMESTAMP, target_date DATE, horizon_days INT, action STRING, score DOUBLE, "
        "confidence DOUBLE, reasons ARRAY<STRING>, model_name STRING, model_version STRING, "
        "features STRING, inputs_max_known_at TIMESTAMP",
    ).createOrReplaceTempView("signal_rows")
    [fn] = [s for s in setup.views_and_functions(N) if ".fn_agent_inputs(" in s]
    body = fn.split(" RETURN ", 1)[1].replace(f"{N.schema('signals')}.signal s", "signal_rows s")
    row = spark.sql(body.replace("fn_agent_inputs.as_of", "s.as_of")).first()
    got = dict(zip(_declared(fn), row, strict=True))
    assert got["signal_action"] == "hold" and got["reasons"] == ["stale_prices"]
    assert got["forecast_anomaly_c"] == 2.97 and got["ret_5d"] is None
    assert got["forecast_known_at"] is not None
    assert got["forecast_known_at"].replace(tzinfo=asof.UTC) <= as_of
    assert got["last_close"] == prices.loc[prices["ticker"] == PRIMARY, "close"].iloc[-1]
    assert got["last_close_date"] == prev
