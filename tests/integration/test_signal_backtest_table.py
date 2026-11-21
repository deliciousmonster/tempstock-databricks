"""`write_backtest` replaces one run id's rows and keeps the others, in a throwaway `dev_pt<run>_signals`."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from tests.conftest import SEED, utc
from tests.unit.tables import prices_at_close
from weather_signal import config, sessions
from weather_signal.jobs.backtest import write_backtest
from weather_signal.signal import backtest
from weather_signal.signal.model import AlwaysHold, ThresholdModel


def _rows(model, now):
    cal = sessions.load(SEED / "sessions.csv")
    days = cal.between(date(2015, 3, 2), date(2015, 3, 6))
    prices = prices_at_close(cal, days[0] - timedelta(days=40), days[-1])
    fc = pd.DataFrame(columns=["target_date", "lead_days", "anomaly_c", "known_at"])
    obs = pd.DataFrame(columns=["date", "anomaly_c", "known_at"])
    return backtest.rows(days, backtest.Inputs(fc, prices, obs), cal, model, now)


def test_rerun_replaces_its_own_rows_only(spark, run_id):
    n = config.Names("workspace", f"dev_pt{run_id}_")
    signals = n.schema("signals")
    spark.sql(f"CREATE SCHEMA {signals}")
    try:
        other = ThresholdModel(1.0, 0.5, model_name="t", model_version="1")
        write_backtest(spark, n, _rows(other, utc(2026, 11, 18, 1)))
        write_backtest(spark, n, _rows(AlwaysHold(), utc(2026, 11, 18, 1)))
        write_backtest(spark, n, _rows(AlwaysHold(), utc(2026, 11, 18, 2)))
        got = spark.sql(
            f"SELECT backtest_run_id, count(*) AS n, max(hour(created_at)) AS h "
            f"FROM {signals}.signal_backtest GROUP BY 1 ORDER BY 1"
        ).collect()
        assert [tuple(r) for r in got] == [("h0:always_hold:1", 5, 2), ("h0:t:1", 5, 1)]
    finally:
        spark.sql(f"DROP SCHEMA IF EXISTS {signals} CASCADE")
