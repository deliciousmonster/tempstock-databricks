"""Backfilled history is known when the plan says, not when the Mac fetched it."""

from datetime import UTC, date, datetime, timedelta

from pyspark.sql import functions as F

from tests.spark.conftest import ts
from tests.spark.helpers import envelopes
from weather_signal.pipeline import transforms

FETCHED = ts(2026, 11, 17, 22)


def _era5(start: date, days: int) -> list[dict]:
    times = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    vals = [10.0] * days
    daily = {"time": times, **{f"temperature_2m_{v}": vals for v in ("mean", "max", "min")}}
    return [{"daily": daily} for _ in range(50)]


def _seen(spark, metros, items, slot):
    days = transforms.era5_days(envelopes(spark, items, slot), metros)
    return {r["date"]: r["known_at"] for r in transforms.era5_first_seen(days).collect()}


def test_era5_backfill_day_is_known_eight_days_after_it(spark, metros):
    seen = _seen(
        spark, metros, [("b", FETCHED, _era5(date(1991, 1, 1), 3))], "1991-01-01..1991-12-31"
    )
    assert seen[date(1991, 1, 1)] == ts(1991, 1, 9)
    assert seen[date(1991, 1, 3)] == ts(1991, 1, 11)


def test_era5_steady_day_keeps_its_first_fetch(spark, metros):
    seen = _seen(spark, metros, [("s", FETCHED, _era5(date(2026, 11, 3), 2))], "2026-11-17")
    assert seen[date(2026, 11, 3)] == FETCHED


def test_era5_takes_the_earlier_of_backfill_and_steady(spark, metros):
    back = envelopes(spark, [("b", FETCHED, _era5(date(2026, 11, 1), 2))], "2026-11-01..2026-11-09")
    steady = envelopes(
        spark, [("s", ts(2026, 11, 8, 3, 5), _era5(date(2026, 11, 1), 2))], "2026-11-08"
    )
    days = transforms.era5_days(back.unionByName(steady), metros)
    seen = {r["date"]: r["known_at"] for r in transforms.era5_first_seen(days).collect()}
    assert seen[date(2026, 11, 1)] == ts(2026, 11, 8, 3, 5)
    assert seen[date(2026, 11, 2)] == ts(2026, 11, 8, 3, 5)


def test_previous_runs_backfill_is_known_from_valid_time_and_lead(spark, metros):
    hours = [f"2021-03-30T{h:02d}:00" for h in range(24)]
    hourly = {"time": hours, "temperature_2m": [5.0] * 24}
    hourly |= {f"temperature_2m_previous_day{n}": [5.0 + n] * 24 for n in range(1, 8)}
    env = envelopes(spark, [("p", FETCHED, [{"hourly": hourly}] * 50)], "2021-03-24..2021-04-06")
    pts = transforms.landed_forecast(transforms.forecast_points(env, metros), 30)
    r = pts.where("lead_days = 2 AND valid_time = '2021-03-30 12:00:00'").first()
    assert r["known_at"] == ts(2021, 3, 28, 12) + timedelta(hours=30)


def test_yahoo_history_bar_is_known_a_day_after_its_close(spark):
    raw = spark.createDataFrame(
        [("yahoo", "WMT", date(2013, 7, 1), 50.0, FETCHED)],
        "source string, ticker string, trade_date date, close double, fetched_at timestamp",
    )
    sessions = spark.createDataFrame(
        [(date(2013, 7, 1), ts(2013, 7, 1, 20))], "session_date date, close_utc timestamp"
    )
    assert transforms.prices_daily(raw, sessions).first()["known_at"] == ts(2013, 7, 2, 20)


def test_era5_backfill_known_at_does_not_follow_the_session_zone(spark, metros):
    spark.conf.set("spark.sql.session.timeZone", "Asia/Tokyo")
    try:
        env = envelopes(
            spark, [("b", FETCHED, _era5(date(1991, 1, 1), 1))], "1991-01-01..1991-12-31"
        )
        days = transforms.era5_days(env, metros)
        (row,) = transforms.era5_first_seen(days).select(F.unix_timestamp("known_at")).collect()
    finally:
        spark.conf.set("spark.sql.session.timeZone", "UTC")
    assert row[0] == int(datetime(1991, 1, 9, tzinfo=UTC).timestamp())


def _in_tokyo(spark, build):
    spark.conf.set("spark.sql.session.timeZone", "Asia/Tokyo")
    try:
        return build()
    finally:
        spark.conf.set("spark.sql.session.timeZone", "UTC")


def _epoch(*a):
    return int(datetime(*a, tzinfo=UTC).timestamp())


def test_previous_runs_valid_time_is_utc_whatever_the_session_zone(spark, metros):
    hourly = {"time": ["2021-03-30T12:00"], "temperature_2m": [5.0]}
    hourly |= {f"temperature_2m_previous_day{n}": [5.0] for n in range(1, 8)}
    env = envelopes(spark, [("p", FETCHED, [{"hourly": hourly}] * 50)], "2021-03-24..2021-04-06")
    row = _in_tokyo(  # the zone binds when the plan is analysed, so build it inside
        spark,
        lambda: (
            transforms.forecast_points(env, metros).select(F.unix_timestamp("valid_time")).first()
        ),
    )
    assert row[0] == _epoch(2021, 3, 30, 12)


def test_era5_bound_is_utc_whatever_the_session_zone(spark):
    df = spark.createDataFrame([(date(1991, 1, 1), _epoch(1991, 1, 5, 20))], ["date", "s"])
    df = df.select("date", F.timestamp_seconds("s").alias("known_at"))
    ok = _in_tokyo(spark, lambda: df.select(transforms.era5_bound_ok()).first())
    assert ok[0] is False


def test_a_session_close_is_read_as_utc_whatever_the_session_zone(spark):
    seed = spark.createDataFrame(
        [("2013-01-02", "2013-01-02T14:30:00", "2013-01-02T21:00:00", "2035-12-31")],
        ["session_date", "open_utc", "close_utc", "end_date"],
    )
    row = _in_tokyo(
        spark, lambda: transforms.sessions(seed).select(F.unix_timestamp("close_utc")).first()
    )
    assert row[0] == _epoch(2013, 1, 2, 21)


def test_forecast_days_are_cut_in_utc_whatever_the_session_zone(spark, metros):
    hours = [f"2026-11-{d}T{h:02d}:00" for d in (20, 21) for h in range(24)]
    hourly = {"time": hours, "temperature_2m": [5.0] * 48}
    hourly |= {f"temperature_2m_previous_day{n}": [5.0] * 48 for n in range(1, 8)}
    env = envelopes(spark, [("p", FETCHED, [{"hourly": hourly}] * 50)], "2026-11-17T23:20:00Z")

    def days():
        pts = transforms.forecast_points(env, metros).withColumn("known_at", F.col("fetched_at"))
        daily = transforms.forecast_daily(pts.where("lead_days = 2"))
        return sorted({r["target_date"] for r in daily.collect()})

    assert _in_tokyo(spark, days) == [date(2026, 11, 20), date(2026, 11, 21)]
