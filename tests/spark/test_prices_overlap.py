"""`prices_daily`: known_at is the earliest agreeing source; disagreement lends none; first close kept."""

from datetime import date

from tests.spark.conftest import ts
from weather_signal.pipeline import transforms

D = date(2026, 11, 16)
SCHEMA = "source string, ticker string, trade_date date, close double, fetched_at timestamp"


def _sessions(spark):
    return spark.createDataFrame(
        [(D, ts(2026, 11, 16, 21)), (date(2026, 11, 13), ts(2026, 11, 13, 21))],
        "session_date date, close_utc timestamp",
    )


def _daily(spark, rows):
    return {
        r["trade_date"]: r
        for r in transforms.prices_daily(
            spark.createDataFrame(rows, SCHEMA), _sessions(spark)
        ).collect()
    }


def test_history_time_when_sources_agree(spark):
    out = _daily(
        spark,
        [
            ("yahoo", "WMT", D, 180.00, ts(2026, 11, 30)),
            ("alphavantage", "WMT", D, 180.01, ts(2026, 12, 1, 0, 5)),
        ],
    )
    assert out[D]["known_at"] == ts(2026, 11, 17, 21)
    assert out[D]["close"] == 180.01 and out[D]["disagreements"] == 0


def test_disagreeing_source_lends_no_time_and_is_counted(spark):
    out = _daily(
        spark,
        [
            ("yahoo", "WMT", D, 170.00, ts(2026, 11, 30)),
            ("alphavantage", "WMT", D, 180.00, ts(2026, 12, 1, 0, 5)),
        ],
    )
    assert out[D]["known_at"] == ts(2026, 12, 1, 0, 5)
    assert out[D]["disagreements"] == 1


def test_first_seen_close_kept_and_revision_counted(spark):
    out = _daily(
        spark,
        [
            ("alphavantage", "WMT", D, 180.00, ts(2026, 11, 16, 22, 5)),
            ("alphavantage", "WMT", D, 181.00, ts(2026, 11, 17, 3, 5)),
        ],
    )
    assert out[D]["close"] == 180.00 and out[D]["revisions"] == 1
    assert out[D]["known_at"] == ts(2026, 11, 16, 22, 5)
