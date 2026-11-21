"""`v_ui_backtest`: the App's History reads the newest backtest run and nothing else."""

from __future__ import annotations

import re
from datetime import date

from weather_signal.catalog.ddl import BACKTEST_VIEW, backtest_view
from weather_signal.config import Names

N = Names("workspace", "dev_")


def _local(sql: str) -> str:
    return re.sub(r"workspace\.dev_(\w+)\.(\w+)", r"\1__\2", sql)


def _rows(run: str, created: str, days: list[int]) -> str:
    feats = '{"forecast_anomaly_c": null, "stock_ret_5d": null, "observed_date": "2015-03-02"}'
    return ", ".join(
        f"('{run}', TIMESTAMP'2015-03-{d} 21:30:00', DATE'2015-03-{d}', 'hold', 0D, 0D, "
        f"array('no_normal'), 'm', '1', '{feats}', TIMESTAMP'2015-03-{d} 00:00:00', 'WMT', 'observed', "
        f"TIMESTAMP'{created}', 0, 0.01D, 0.0D, 0.0D, 0.0D, false, 2, 0)"
        for d in days
    )


def test_the_view_keeps_only_the_newest_run(spark):
    old, new = (
        _rows("h0:a:1", "2026-11-18 10:00:00", [10, 11]),
        _rows("h0:b:1", "2026-11-18 11:00:00", [10]),
    )
    spark.sql(
        f"CREATE OR REPLACE TEMP VIEW signals__signal_backtest AS SELECT * FROM VALUES {old}, {new} "
        "AS t(backtest_run_id, as_of, target_date, action, score, confidence, reasons, model_name, "
        "model_version, features, inputs_max_known_at, ticker, input_kind, created_at, pos, ret, "
        "pnl, gate_total, gate_p95, gate_pass, gate_n, gate_trades)"
    )
    spark.sql(
        "CREATE OR REPLACE TEMP VIEW daily__prices_daily AS SELECT * FROM VALUES "
        "('WMT', DATE'2015-03-10', 40D), ('SPY', DATE'2015-03-10', 600D) "
        "AS t(ticker, trade_date, close)"
    )
    r = spark.sql(_local(backtest_view(N))).collect()
    assert BACKTEST_VIEW == "v_ui_backtest"
    assert [(x.backtest_run_id, x.target_date, x.ticker, x.close) for x in r] == [
        ("h0:b:1", date(2015, 3, 10), "WMT", 40.0)
    ]
    assert (r[0].anomaly_c, r[0].observed_date) == (None, date(2015, 3, 2))
