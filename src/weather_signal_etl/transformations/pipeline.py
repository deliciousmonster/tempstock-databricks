"""The declarative pipeline, wiring only: every transform is in `weather_signal.pipeline.transforms`."""

from importlib import resources

import dlt
import pandas as pd
from pyspark.sql import functions as F
from pyspark.sql import types as T

from weather_signal import config
from weather_signal.ingest import intraday, price_bars
from weather_signal.pipeline import transforms
from weather_signal.sessions import load as load_calendar
from weather_signal.signal import checks

conf = spark.conf  # the pipeline runtime provides `spark`
CATALOG = conf.get("weather_signal.catalog")
PREFIX = conf.get("weather_signal.schema_prefix", "")
P_HOURS = int(conf.get("weather_signal.p_hours"))
RAW = conf.get("weather_signal.raw")


def fq(schema: str, name: str) -> str:
    return f"{CATALOG}.{PREFIX}{schema}.{name}"


def _seed(name: str) -> pd.DataFrame:
    with resources.files("weather_signal").joinpath(f"seed/{name}").open() as f:
        return pd.read_csv(f, dtype={"cbsa_code": str})


# raw ---------------------------------------------------------------------------------------


@dlt.table(name=fq("raw", "raw_envelopes"), comment="Envelopes as landed, hash-checked")
@dlt.expect_all_or_drop({"sha_ok": "sha_ok", "http_200": "http_status = 200"})
def raw_envelopes():
    df = (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "json")
        .schema(transforms.ENVELOPE_SCHEMA)
        .load(RAW)
    )
    return transforms.raw(df, F.col("_metadata.file_path"))


def _envelopes(stream: bool, source: str):
    t = fq("raw", "raw_envelopes")
    df = spark.readStream.table(t) if stream else spark.read.table(t)
    return df.where(F.col("source") == source)


# reference ------------------------------------------------------------------------------------


@dlt.table(name=fq("daily", "metros"))
@dlt.expect_all_or_fail(
    {"fifty": "n = 50", "weights": "abs(w - 1) < 1e-4", "continental": "lat_ok AND lon_ok"}
)
def metros_check():
    m = spark.createDataFrame(_seed("metros.csv"))
    return (
        m.crossJoin(m.agg(F.count("*").alias("n"), F.sum("weight").alias("w")))
        .withColumn("lat_ok", F.col("latitude").between(24, 50))
        .withColumn("lon_ok", F.col("longitude").between(-125, -66))
    )


def metros():
    return spark.read.table(fq("daily", "metros")).select("rank", "cbsa_code", "weight")


@dlt.table(name=fq("daily", "sessions"))
def sessions():
    s = _seed("sessions.csv")
    return transforms.sessions(spark.createDataFrame(s))


# forecast -------------------------------------------------------------------------------------


@dlt.view(name="forecast_landed")
def forecast_landed():
    pts = transforms.forecast_points(_envelopes(True, "open-meteo-previous-runs"), metros())
    return transforms.landed_forecast(pts, P_HOURS).withColumn(
        "seq", transforms.first_sighting_sequence()
    )


dlt.create_streaming_table(
    name=fq("daily", "forecast_hourly"),
    expect_all_or_drop={"temp_bounds": "temp_c BETWEEN -60 AND 60"},
)
dlt.create_auto_cdc_flow(
    target=fq("daily", "forecast_hourly"),
    source="forecast_landed",
    keys=transforms.FORECAST_KEY,
    sequence_by=F.col("seq"),
    stored_as_scd_type=1,
)


@dlt.view(name="v_forecast_fill_runs")
def v_forecast_fill_runs():
    pts = transforms.forecast_points(_envelopes(False, "open-meteo-previous-runs"), metros())
    return transforms.fill_runs(transforms.landed_forecast(pts, P_HOURS))


@dlt.table(name=fq("daily", "forecast_fill_check"))
@dlt.expect("lead_not_filled", f"max_run < {transforms.FILL_RUN}")
def forecast_fill_check():
    """Counted in the event log, not halting: a long match can be the weather (2022-04-29, 41860)."""
    return spark.read.table("v_forecast_fill_runs")


@dlt.table(name=fq("daily", "forecast_fill_quarantine"))
def forecast_fill_quarantine():
    pts = transforms.forecast_points(_envelopes(False, "open-meteo-previous-runs"), metros())
    return transforms.fill_quarantine(transforms.landed_forecast(pts, P_HOURS))


@dlt.table(name=fq("daily", "forecast_fill_summary"))
@dlt.expect("fill_one_metro", f"n_metros < {checks.FILL_ALERT_METROS}")
def forecast_fill_summary():
    return transforms.fill_summary(spark.read.table(fq("daily", "forecast_fill_quarantine")))


@dlt.table(name=fq("daily", "forecast_quarantined_days"))
def forecast_quarantined_days():
    hourly = spark.read.table(fq("daily", "forecast_hourly"))
    q = spark.read.table(fq("daily", "forecast_fill_quarantine"))
    return transforms.quarantined_days(hourly, q)


@dlt.table(name=fq("daily", "forecast_daily"))
def forecast_daily():
    hourly = spark.read.table(fq("daily", "forecast_hourly"))
    q = spark.read.table(fq("daily", "forecast_fill_quarantine"))
    return transforms.forecast_daily(transforms.drop_quarantined(hourly, q))


# ERA5 -----------------------------------------------------------------------------------------


@dlt.table(name=fq("daily", "era5_first_seen"))
def era5_first_seen():
    days = transforms.era5_days(_envelopes(False, "open-meteo-era5"), metros())
    return transforms.era5_first_seen(days)


@dlt.view(name="era5_landed")
def era5_landed():
    days = transforms.era5_days(_envelopes(True, "open-meteo-era5"), metros())
    return days.where("t_mean IS NOT NULL").drop("slot")


dlt.create_streaming_table(
    name=fq("daily", "weather_observed_daily"),
    expect_all_or_drop={
        "bounds": "t_mean BETWEEN -60 AND 60",
        "order": "t_min IS NULL OR t_max IS NULL OR t_min <= t_max",
    },
)
dlt.create_auto_cdc_flow(
    target=fq("daily", "weather_observed_daily"),
    source="era5_landed",
    keys=["cbsa_code", "date"],
    sequence_by=F.col("fetched_at"),
    stored_as_scd_type=1,
)


# prices ---------------------------------------------------------------------------------------

BAR = T.ArrayType(
    T.StructType(
        [
            T.StructField("ticker", T.StringType()),
            T.StructField("trade_date", T.DateType()),
            T.StructField("close", T.DoubleType()),
            T.StructField("high", T.DoubleType()),
            T.StructField("low", T.DoubleType()),
        ]
    )
)


@F.udf(BAR)
def _bars(source, endpoint, params, body_text):
    bars = price_bars.from_envelope(source, endpoint, dict(params or {}), body_text)
    return [(b.ticker, b.trade_date, b.close, b.high, b.low) for b in bars]


@dlt.table(name=fq("daily", "prices_raw"))
@dlt.expect_all_or_drop({"close_positive": "close > 0", "order": "low <= close AND close <= high"})
def prices_raw():
    env = spark.readStream.table(fq("raw", "raw_envelopes")).where(
        F.col("source").isin("alphavantage", "yahoo")
    )
    b = env.select(
        "source",
        "fetched_at",
        F.explode(_bars("source", "request.endpoint", "request.params", "body_text")).alias("b"),
    )
    return b.select("source", "b.*", "fetched_at")


@dlt.table(name=fq("daily", "prices_daily"))
@dlt.expect("no_revision", "revisions = 0")
@dlt.expect("sources_agree", "disagreements = 0")
@dlt.expect_or_fail("split_guard", transforms.SPLIT_OK)
@dlt.expect_or_fail("known_at_bound", transforms.PRICE_KNOWN_AT_OK)
@dlt.expect_or_fail("in_calendar", "close_utc IS NOT NULL")
def prices_daily():
    raw = spark.read.table(fq("daily", "prices_raw"))
    return transforms.prices_daily(raw, spark.read.table(fq("daily", "sessions")))


# intraday (display only: no model input reads it) -------------------------------------------

INTRADAY_BAR = T.ArrayType(
    T.StructType(
        [
            T.StructField("ticker", T.StringType()),
            T.StructField("bar_start_s", T.LongType()),
            T.StructField("open", T.DoubleType()),
            T.StructField("high", T.DoubleType()),
            T.StructField("low", T.DoubleType()),
            T.StructField("close", T.DoubleType()),
            T.StructField("volume", T.LongType()),
            T.StructField("final", T.BooleanType()),
            T.StructField("keep_from_s", T.LongType()),
        ]
    )
)


@F.udf(INTRADAY_BAR)
def _intraday_bars(body_text, fetched_s):
    return intraday.udf_rows(body_text, fetched_s, load_calendar())


@dlt.table(
    name=fq("daily", "prices_intraday"),
    comment="WMT 60m bars from Yahoo for display; the last 5 sessions; never a model input",
)
@dlt.expect_all_or_drop({"intraday_close_positive": "close > 0"})
def prices_intraday():
    env = _envelopes(False, intraday.SOURCE)
    b = env.select(
        "path",
        "fetched_at",
        F.explode(_intraday_bars("body_text", F.unix_timestamp("fetched_at"))).alias("b"),
    )
    bars = b.select(
        "path",
        "fetched_at",
        F.timestamp_seconds("b.keep_from_s").alias("keep_from"),
        "b.ticker",
        F.timestamp_seconds("b.bar_start_s").alias("bar_start"),
        "b.open",
        "b.high",
        "b.low",
        "b.close",
        "b.volume",
        "b.final",
    )
    return transforms.prices_intraday(bars, intraday.TABLE_SOURCE)


# signals -----------------------------------------------------------------------------------------


@dlt.table(name=fq("signals", "temp_index_daily"))
def temp_index_daily():
    obs = spark.read.table(fq("daily", "weather_observed_daily"))
    return transforms.temp_index_daily(obs, metros())


@dlt.table(name=fq("signals", "normals"))
def normals():
    closes = spark.read.table(fq("daily", "prices_daily")).where(F.col("ticker") == config.PRIMARY)
    return transforms.normals(spark.read.table(fq("signals", "temp_index_daily")), closes)


@dlt.table(name=fq("signals", "forecast_index"))
def forecast_index():
    daily = spark.read.table(fq("daily", "forecast_daily"))
    norms = spark.read.table(fq("signals", "normals"))
    q = spark.read.table(fq("daily", "forecast_quarantined_days"))
    return transforms.forecast_index(daily, metros(), norms, q)
