"""`prices_intraday`: each envelope replaces rows from its oldest bar on; retention is 5 sessions."""

from __future__ import annotations

from datetime import date

from pyspark.sql import functions as F

from tests.conftest import utc
from tests.spark.conftest import ts
from tests.unit.yahoo_60m import body, session_points
from weather_signal.config import Names
from weather_signal.ingest import intraday
from weather_signal.pipeline import transforms
from weather_signal.serving import views

SCHEMA = (
    "path string, fetched_at timestamp, keep_from timestamp, ticker string, bar_start timestamp, "
    "open double, high double, low double, close double, volume bigint, final boolean"
)
KEEP = ts(2026, 11, 16, 14, 30)


def _bar(path, fetched, start, close, final=True, keep=KEEP):
    return (path, fetched, keep, "WMT", start, close, close, close, close, 10, final)


def _table(spark, rows):
    df = spark.createDataFrame(rows, SCHEMA)
    out = transforms.prices_intraday(df, intraday.TABLE_SOURCE).collect()
    return {r["bar_start"]: r for r in out}


def test_a_later_envelope_replaces_rows_from_its_oldest_bar_on(spark):
    a, b = ts(2026, 11, 20, 17, 5), ts(2026, 11, 20, 18, 5)
    got = _table(
        spark,
        [
            _bar("a", a, ts(2026, 11, 20, 15, 30), 100.0),
            _bar("a", a, ts(2026, 11, 20, 16, 30), 101.0, final=False),
            _bar("a", a, ts(2026, 11, 20, 17, 30), 99.0, final=False),  # absent from b: dropped
            _bar("b", b, ts(2026, 11, 20, 16, 30), 101.5),
        ],
    )
    assert set(got) == {ts(2026, 11, 20, 15, 30), ts(2026, 11, 20, 16, 30)}
    assert got[ts(2026, 11, 20, 15, 30)]["close"] == 100.0  # older than b's first bar: kept
    newer = got[ts(2026, 11, 20, 16, 30)]
    assert newer["close"] == 101.5 and newer["final"] and newer["fetched_at"] == b


def test_retention_follows_the_newest_envelopes_keep_from(spark):
    old, new = ts(2026, 11, 13, 20, 5), ts(2026, 11, 20, 15, 5)
    got = _table(
        spark,
        [
            _bar("old", old, ts(2026, 11, 13, 19, 30), 90.0, keep=ts(2026, 11, 9, 14, 30)),
            _bar("new", new, ts(2026, 11, 16, 14, 30), 95.0),
            _bar("new", new, ts(2026, 11, 13, 20, 30), 94.0),
        ],
    )
    assert list(got) == [ts(2026, 11, 16, 14, 30)]


def test_the_table_carries_the_contracted_columns_and_source(spark):
    got = _table(spark, [_bar("a", ts(2026, 11, 20, 15, 5), ts(2026, 11, 20, 14, 30), 100.0)])
    (row,) = got.values()
    assert tuple(row.asDict()) == transforms.INTRADAY_COLUMNS
    assert row["source"] == "yahoo_chart_60m"


def test_the_udf_rows_round_trip_through_spark_as_utc(spark, cal):
    now = utc(2026, 11, 20, 18, 5)
    pts = session_points(cal, [date(2026, 11, 19), date(2026, 11, 20)], now)
    rows = intraday.udf_rows(body(pts).decode(), int(now.timestamp()), cal)
    df = spark.createDataFrame(
        rows,
        "ticker string, bar_start_s long, open double, high double, low double, close double, "
        "volume long, final boolean, keep_from_s long",
    ).select(
        F.lit("p").alias("path"),
        F.lit(ts(2026, 11, 20, 18, 5)).alias("fetched_at"),
        F.timestamp_seconds("keep_from_s").alias("keep_from"),
        "ticker",
        F.timestamp_seconds("bar_start_s").alias("bar_start"),
        "open",
        "high",
        "low",
        "close",
        "volume",
        "final",
    )
    out = transforms.prices_intraday(df, intraday.TABLE_SOURCE).orderBy("bar_start").collect()
    assert out[0]["bar_start"] == ts(2026, 11, 19, 14, 30) and len(out) == 7 + 4
    assert [r["final"] for r in out[-2:]] == [True, False]


def test_the_pipeline_declares_the_table_and_its_check_only_drops(spark):
    from tests.spark.pipeline_graph import load

    assert load()["prices_intraday"] == [
        ("expect_all_or_drop", "intraday_close_positive", "close > 0")
    ]


def test_the_view_carries_each_bars_session_close_from_the_calendar(spark):
    n = Names("workspace", "dev_")
    spark.createDataFrame(
        [("WMT", ts(2026, 11, 27, 17, 30), 100.0, 5, True, ts(2026, 11, 27, 18, 5))],
        "ticker string, bar_start timestamp, close double, volume bigint, final boolean, "
        "fetched_at timestamp",
    ).createOrReplaceTempView("daily__prices_intraday")
    spark.createDataFrame(
        [(date(2026, 11, 27), ts(2026, 11, 27, 14, 30), ts(2026, 11, 27, 18), date(2026, 11, 27))],
        "session_date date, open_utc timestamp, close_utc timestamp, end_date date",
    ).createOrReplaceTempView("daily__sessions")
    sql = views.intraday(n).replace("workspace.dev_daily.", "daily__")
    (row,) = spark.sql(sql).collect()
    assert row["session_close"] == ts(2026, 11, 27, 18) and row["close"] == 100.0
    assert row["session_date"] == date(2026, 11, 27) and row["fetched_at"] > row["bar_start"]
