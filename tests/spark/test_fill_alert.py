"""The fill alert's thresholds as the pipeline and the `check` task evaluate them, on Spark rows."""

import time
from datetime import UTC, datetime, timedelta

from tests.spark.pipeline_graph import load
from weather_signal import config
from weather_signal.jobs.score import metrics


def test_fill_one_metro_passes_one_metro_and_fails_five(spark):
    cond = {n: c for _, n, c in load()["forecast_fill_summary"]}["fill_one_metro"]
    df = spark.createDataFrame([(1,), (4,), (5,), (50,)], "n_metros int")
    assert sorted(r["n_metros"] for r in df.where(cond).collect()) == [1, 4]


def test_the_check_task_reads_the_summary_spark_returns(spark, tmp_path, monkeypatch):
    """`collect()` gives naive timestamps, in local time on a Connect client; the Mac's zone
    here, with a fill 6 d 20 h old that a local-as-UTC reading would put past the window."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    n = config.Names("spark_catalog", "t7_")
    daily, ops = n.schema("daily"), n.schema("ops")
    spark.sql(f"CREATE DATABASE IF NOT EXISTS {daily} LOCATION '{tmp_path / 'daily'}'")
    spark.sql(f"CREATE DATABASE IF NOT EXISTS {ops} LOCATION '{tmp_path / 'ops'}'")
    try:
        spark.sql(
            f"CREATE TABLE {daily}.prices_daily (ticker string, trade_date date, disagreements int)"
        )
        fresh = datetime.now(UTC) - timedelta(days=6, hours=20)
        rows = [("p", 7, 50, 300, fresh), ("q", 3, 1, 6, fresh)]
        schema = "path string, lead_days int, n_metros int, hours int, fetched_at timestamp"
        spark.createDataFrame(rows, schema).write.saveAsTable(f"{daily}.forecast_fill_summary")
        found = metrics(spark, n, datetime.now(UTC).date())
        assert [(f.level, f.name) for f in found if f.name == "forecast_filled"] == [
            ("fail", "forecast_filled")
        ]
        again = metrics(spark, n, datetime.now(UTC).date())
        assert "forecast_filled" not in {f.name for f in again}
        alerted = spark.sql(f"SELECT path FROM {n.schema('ops')}.fill_alerts").collect()
        assert [r["path"] for r in alerted] == ["p"]
    finally:
        spark.sql(f"DROP DATABASE IF EXISTS {daily} CASCADE")
        spark.sql(f"DROP DATABASE IF EXISTS {ops} CASCADE")
        monkeypatch.undo()
        time.tzset()
