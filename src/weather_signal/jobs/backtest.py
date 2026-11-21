"""`weather-signal-backtest`: the backtest job's one task, written to `signals.signal_backtest`."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from weather_signal import config
from weather_signal.catalog.ddl import BACKTEST_COLUMNS, add_columns_sql
from weather_signal.catalog.ddl import signal_backtest as signal_backtest_ddl
from weather_signal.jobs.task import TaskFailed, task_args
from weather_signal.sessions import load as load_calendar
from weather_signal.signal import backtest
from weather_signal.spark import spark_session

log = logging.getLogger(__name__)


def _epoch_known(df: Any) -> Any:
    """Collected timestamps are naive in the client's zone; epoch seconds carry no zone."""
    pdf = df.toPandas()
    # Connect attaches query metrics to attrs, which pandas deep-copies on every operation.
    pdf.attrs.clear()
    pdf["known_at"] = pd.to_datetime(pdf.pop("known_s"), unit="s", utc=True)
    return pdf


def backtest_inputs(spark: Any, n: config.Names) -> backtest.Inputs:
    signals, daily = n.schema("signals"), n.schema("daily")
    since = (backtest.START - timedelta(days=40)).isoformat()
    fc = spark.sql(
        "SELECT target_date, lead_days, anomaly_c, quarantined, unix_timestamp(known_at) AS known_s "
        f"FROM {signals}.forecast_index WHERE lead_days = {backtest.LEAD}"
    )
    px = spark.sql(
        "SELECT ticker, trade_date, close, unix_timestamp(known_at) AS known_s "
        f"FROM {daily}.prices_daily WHERE trade_date >= DATE'{since}' AND known_at IS NOT NULL"
    )
    obs = spark.sql(
        "SELECT t.date, t.index_c - m.normal_c AS anomaly_c, unix_timestamp(e.known_at) AS known_s "
        f"FROM {signals}.temp_index_daily t JOIN {daily}.era5_first_seen e ON e.date = t.date "
        f"LEFT JOIN {signals}.normals m ON m.day_of_year = dayofyear(t.date) "
        f"WHERE t.date >= DATE'{since}'"
    )
    return backtest.Inputs(_epoch_known(fc), _epoch_known(px), _epoch_known(obs))


def write_backtest(spark: Any, n: config.Names, rows: list[dict[str, Any]]) -> None:
    """Replace the rows of this run id in one commit; other runs' rows stay."""
    from pyspark.sql import Row
    from pyspark.sql import functions as F

    table = n.table("signals", "signal_backtest")
    spark.sql(signal_backtest_ddl(n))
    add = add_columns_sql(table, BACKTEST_COLUMNS, spark.table(table).columns)
    if add:
        spark.sql(add)
    ids = {r["backtest_run_id"] for r in rows}
    if len(ids) != 1:
        raise TaskFailed(f"backtest: {len(ids)} run ids in one write")
    df = spark.createDataFrame([Row(**r) for r in rows], schema=spark.table(table).schema)
    df.writeTo(table).overwrite(F.col("backtest_run_id") == ids.pop())


def backtest_task(spark: Any, n: config.Names, now: datetime) -> int:
    inp = backtest_inputs(spark, n)
    days = backtest.sessions(inp.prices, now)
    w = backtest.walk(days, inp, load_calendar(), now)
    rows = w.rows
    if not rows:
        raise TaskFailed("no_signal_rows: no session to backtest")
    s = backtest.summary(rows)
    log.info("backtest summary %s; gate %s", s, w.verdict)
    try:
        backtest.check_missing(s)
    except backtest.TooManyMissing as e:
        raise TaskFailed(f"backtest_missing_input: {e}") from e
    write_backtest(spark, n, rows)
    return len(rows)


def main(argv: Sequence[str] | None = None) -> int:
    a = task_args("weather-signal-backtest", argv)
    logging.basicConfig(level=logging.INFO)
    n = config.Names(a.catalog, a.schema_prefix)
    log.info(
        "backtest wrote %d rows", backtest_task(spark_session(a.profile), n, datetime.now(UTC))
    )
    return 0
