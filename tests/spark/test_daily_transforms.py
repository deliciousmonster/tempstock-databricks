"""Forecast daily means need 24 hours; the split guard flags a 40% move."""

from datetime import date, timedelta

from pyspark.sql import functions as F

from tests.spark.conftest import ts
from weather_signal.pipeline import transforms


def test_forecast_daily_needs_24_hours(spark):
    rows = [
        ("1", ts(2026, 11, 20) + timedelta(hours=h), 3, float(h), ts(2026, 11, 18))
        for h in range(24)
    ]
    rows += [
        ("1", ts(2026, 11, 21) + timedelta(hours=h), 3, 1.0, ts(2026, 11, 19)) for h in range(23)
    ]
    df = spark.createDataFrame(
        rows,
        "cbsa_code string, valid_time timestamp, lead_days int, temp_c double, known_at timestamp",
    )
    out = transforms.forecast_daily(df).collect()
    assert len(out) == 1 and out[0]["target_date"] == date(2026, 11, 20)
    assert out[0]["temp_c"] == sum(range(24)) / 24


def test_split_guard(spark):
    df = spark.createDataFrame([(None,), (0.1,), (-0.39,), (0.5,), (-0.45,)], "ret double")
    assert [r["ret"] for r in df.where(~F.expr(transforms.SPLIT_OK)).collect()] == [0.5, -0.45]
    assert df.where(F.expr(transforms.SPLIT_OK)).count() == 3
