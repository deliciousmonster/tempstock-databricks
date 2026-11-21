"""A backtest over a frozen ERA5 table holds past the age limit, read back through `v_ui_backtest`
(2026-11-20 dev: 1,369 WMT buys from 2008 to 2013 all read the 2007-12-27 observation)."""

from __future__ import annotations

import re
from datetime import date, timedelta

from tests.conftest import utc
from tests.unit.tables import prices_at_close
from weather_signal import asof
from weather_signal.catalog.ddl import BACKTEST_COLUMNS, backtest_view
from weather_signal.config import Names
from weather_signal.jobs.backtest import backtest_inputs
from weather_signal.signal import backtest
from weather_signal.signal.model import ThresholdModel

N = Names("workspace", "dev_")
LAST_ERA5 = date(2007, 12, 27)


def _local(sql: str) -> str:
    return re.sub(r"workspace\.dev_(\w+)\.(\w+)", r"\1__\2", sql)


class _Local:
    """The job's SQL against temp views named `<schema>__<table>`."""

    def __init__(self, spark) -> None:
        self.spark = spark

    def sql(self, q: str):
        return self.spark.sql(_local(q))


def _view(spark, name: str, rows: list[tuple], schema: str) -> None:
    spark.createDataFrame(rows, schema).createOrReplaceTempView(name)


def _frozen_world(spark, cal, days: list[date]) -> None:
    era5 = [LAST_ERA5 - timedelta(days=i) for i in range(30)]
    _view(
        spark,
        "signals__forecast_index",
        [],
        "target_date DATE, lead_days INT, anomaly_c DOUBLE, quarantined BOOLEAN, known_at TIMESTAMP",
    )
    px = prices_at_close(cal, days[0] - timedelta(days=60), days[-1])
    _view(
        spark,
        "daily__prices_daily",
        [(t, d, c, k.to_pydatetime()) for t, d, c, k in px.itertuples(index=False, name=None)],
        "ticker STRING, trade_date DATE, close DOUBLE, known_at TIMESTAMP",
    )
    _view(
        spark, "signals__temp_index_daily", [(d, 13.0) for d in era5], "date DATE, index_c DOUBLE"
    )
    _view(
        spark,
        "daily__era5_first_seen",
        [(d, asof.known_at_era5_backfill(d)) for d in era5],
        "date DATE, known_at TIMESTAMP",
    )
    _view(
        spark,
        "signals__normals",
        [(i, 10.0) for i in range(1, 367)],
        "day_of_year INT, normal_c DOUBLE",
    )


def test_a_frozen_observation_holds_past_the_limit_in_the_view(spark, cal):
    days = cal.between(date(2008, 1, 2), date(2008, 1, 31))
    _frozen_world(spark, cal, days)
    inp = backtest_inputs(_Local(spark), N)
    model = ThresholdModel(1.0, 0.5, model_name="m", model_version="1")
    rows = backtest.rows(days, inp, cal, model, utc(2026, 11, 20, 12))
    schema = BACKTEST_COLUMNS.replace(" NOT NULL", "")
    cols = [c.split()[0] for c in schema.split(", ")]
    _view(spark, "signals__signal_backtest", [tuple(r[c] for c in cols) for r in rows], schema)
    got = spark.sql(_local(backtest_view(N))).collect()
    assert len(got) == len(days)
    for r in got:
        stale = (r.target_date - r.observed_date).days > 14
        assert r.observed_date <= LAST_ERA5 and r.anomaly_c == 3.0
        assert (r.action, r.reasons) == (("hold", ["stale_input"]) if stale else ("buy", []))
    assert sum(r.action == "buy" for r in got) == 7
    assert min(r.target_date for r in got if r.action == "hold") == date(2008, 1, 11)
