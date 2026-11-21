"""The indexes drop any day missing a metro; normals need every 1961-1990 year, as pandas does."""

from datetime import date, timedelta

import pandas as pd
import pytest

from weather_signal.pipeline import transforms
from weather_signal.signal import temperature_index


def test_index_drops_partial_days(spark, metros):
    codes = [r["cbsa_code"] for r in metros.collect()]
    rows = [(c, date(2026, 11, 1), 10.0) for c in codes] + [
        (c, date(2026, 11, 2), 10.0) for c in codes[:49]
    ]
    obs = spark.createDataFrame(rows, "cbsa_code string, date date, t_mean double")
    out = transforms.temp_index_daily(obs, metros).collect()
    assert [r["date"] for r in out] == [date(2026, 11, 1)] and abs(out[0]["index_c"] - 10.0) < 1e-9


def test_forecast_index_drops_days_under_the_metro_minimum_and_subtracts_normals(spark, metros):
    codes = [r["cbsa_code"] for r in metros.collect()]
    rows = [(c, date(2026, 11, 20), 3, 12.0, None) for c in codes]
    rows += [(c, date(2026, 11, 21), 3, 12.0, None) for c in codes[6:]]
    daily = spark.createDataFrame(
        rows, "cbsa_code string, target_date date, lead_days int, temp_c double, known_at timestamp"
    )
    norms = spark.createDataFrame([(324, 10.0), (325, 10.0)], "day_of_year int, normal_c double")
    none = spark.createDataFrame(
        [], "cbsa_code string, target_date date, lead_days int, known_at timestamp"
    )
    out = transforms.forecast_index(daily, metros, norms, none).collect()
    assert len(out) == 1 and abs(out[0]["anomaly_c"] - 2.0) < 1e-9


def _doy(y: int, doy: int) -> date:
    return date(y, 1, 1) + timedelta(days=doy - 1)


def test_normals_need_every_window_year_and_agree_with_pandas(spark):
    leap = [y for y in range(1961, 1991) if y % 4 == 0]
    rows = [(_doy(y, 100), float(y % 7)) for y in range(1950, 2026)]
    rows += [(_doy(y, 101), 1.0) for y in range(1961, 1991) if y != 1975]
    rows += [(_doy(y, 366), float(y % 5)) for y in leap]
    df = spark.createDataFrame(rows, "date date, index_c double")
    got = {r["day_of_year"]: r["normal_c"] for r in transforms.normals(df).collect()}
    want = temperature_index.normals(pd.DataFrame(rows, columns=["date", "index_c"])).set_index(
        "day_of_year"
    )
    assert got[101] is None and pd.isna(want.loc[101, "normal_c"])
    for doy in (100, 366):
        assert got[doy] == pytest.approx(want.loc[doy, "normal_c"]), doy
    assert got[100] == pytest.approx(sum(y % 7 for y in range(1961, 1991)) / 30)
    assert got[366] == pytest.approx(sum(y % 5 for y in leap) / len(leap))


def _closes(spark, days):
    return spark.createDataFrame([(d,) for d in days], "trade_date date")


def test_normals_keep_1961_1990_when_every_year_is_there(spark):
    rows = [(_doy(y, 100), float(y % 7)) for y in range(1961, 2001)]
    df = spark.createDataFrame(rows, "date date, index_c double")
    closes = [_doy(y, 100) for y in range(1991, 2001)]
    got = transforms.normals(df, _closes(spark, closes)).collect()
    assert {r["period"] for r in got} == {"1961-1990"}


def test_normals_without_the_window_come_from_the_training_half_and_agree_with_pandas(spark):
    # 2000-2009 index on days 100 and 200, closes on day 100: the middle session is 2005's.
    rows = [(_doy(y, d), float(y % 7 + d)) for y in range(2000, 2010) for d in (100, 200)]
    closes = [_doy(y, 100) for y in range(2000, 2010)]
    df = spark.createDataFrame(rows, "date date, index_c double")
    got = {r["day_of_year"]: r for r in transforms.normals(df, _closes(spark, closes)).collect()}
    pdf = pd.DataFrame(rows, columns=["date", "index_c"])
    want = temperature_index.normals(pdf, pd.Series(pd.to_datetime(closes))).set_index(
        "day_of_year"
    )
    assert {r["period"] for r in got.values()} == {"2000-2005 (training half)"}
    assert want["period"].unique().tolist() == ["2000-2005 (training half)"]
    for doy in (100, 200):
        assert got[doy]["normal_c"] is not None
        assert got[doy]["normal_c"] == pytest.approx(want.loc[doy, "normal_c"]), doy
    # Day 100 of 2000-2004 only: the middle session (2005) and after stay for evaluation.
    assert got[100]["normal_c"] == pytest.approx(sum(y % 7 + 100 for y in range(2000, 2005)) / 5)
