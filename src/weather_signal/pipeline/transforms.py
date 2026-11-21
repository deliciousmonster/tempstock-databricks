"""Plain PySpark; the declarative pipeline in src/weather_signal_etl only wires these together."""

from __future__ import annotations

from pyspark.sql import Column, DataFrame, Window
from pyspark.sql import functions as F
from pyspark.sql import types as T

from weather_signal.asof import ERA5_BACKFILL_DELAY
from weather_signal.ingest.validate import SPLIT_GUARD
from weather_signal.signal.temperature_index import (
    MIN_FORECAST_WEIGHT,
    N_METROS,
    NORMALS_YEARS,
    expected_years,
)

# The prices_daily expectations, as SQL: the pipeline declares them and tests/spark evaluates them.
SPLIT_OK = f"ret IS NULL OR abs(ret) < {SPLIT_GUARD}"
PRICE_KNOWN_AT_OK = "known_at IS NULL OR known_at >= close_utc + INTERVAL 15 MINUTES"

ENVELOPE_SCHEMA = T.StructType(
    [
        T.StructField("source", T.StringType()),
        T.StructField(
            "request",
            T.StructType(
                [
                    T.StructField("endpoint", T.StringType()),
                    T.StructField("params", T.MapType(T.StringType(), T.StringType())),
                ]
            ),
        ),
        T.StructField("slot", T.StringType()),
        T.StructField("host", T.StringType()),
        T.StructField("fetched_at", T.TimestampType()),
        T.StructField("http_status", T.IntegerType()),
        T.StructField("ratelimit_cost", T.DoubleType()),
        T.StructField("body_sha256", T.StringType()),
        T.StructField("body_text", T.StringType()),
    ]
)
LEADS = range(1, 8)
HOURLY = T.StructType(
    [
        T.StructField("time", T.ArrayType(T.StringType())),
        T.StructField("temperature_2m", T.ArrayType(T.DoubleType())),
    ]
    + [T.StructField(f"temperature_2m_previous_day{n}", T.ArrayType(T.DoubleType())) for n in LEADS]
)
DAILY = T.StructType(
    [
        T.StructField(c, T.ArrayType(T.StringType() if c == "time" else T.DoubleType()))
        for c in ("time", "temperature_2m_mean", "temperature_2m_max", "temperature_2m_min")
    ]
)
AGREE_WITHIN = 0.005


def raw(df: DataFrame, path: Column) -> DataFrame:
    """The envelope as landed, its hash checked, its body parsed once to VARIANT."""
    return df.select(
        "*",
        path.alias("path"),
        (F.sha2(F.col("body_text"), 256) == F.col("body_sha256")).alias("sha_ok"),
        F.parse_json("body_text").alias("body"),
    )


def _locations(schema: T.StructType, field: str) -> Column:
    arr = T.ArrayType(T.StructType([T.StructField(field, schema)]))
    text = F.when(F.col("body_text").startswith("["), F.col("body_text")).otherwise(
        F.concat(F.lit("["), F.col("body_text"), F.lit("]"))
    )
    return F.from_json(text, arr)


def forecast_points(env: DataFrame, metros: DataFrame) -> DataFrame:
    """Every (envelope, metro, hour, lead) value with that envelope's lead 0, before any filter."""
    locs = env.select(
        "path", "fetched_at", F.posexplode(_locations(HOURLY, "hourly")).alias("pos", "loc")
    )
    h = F.col("loc.hourly")
    hours = locs.select(
        "path",
        "fetched_at",
        (F.col("pos") + 1).alias("rank"),
        F.posexplode(h["time"]).alias("i", "t"),
        h.alias("h"),
    )
    leads = F.array(
        *[
            F.struct(
                F.lit(n).alias("lead_days"),
                F.col("h")[f"temperature_2m_previous_day{n}"][F.col("i")].alias("temp_c"),
            )
            for n in LEADS
        ]
    )
    out = (
        hours.select(
            "path",
            "fetched_at",
            "rank",
            utc_wall(F.col("t")).alias("valid_time"),
            F.col("h")["temperature_2m"][F.col("i")].alias("lead0_c"),
            F.explode(leads).alias("l"),
        )
        .select("*", "l.lead_days", "l.temp_c")
        .drop("l")
    )
    return out.join(metros.select("rank", "cbsa_code"), "rank").where(F.col("temp_c").isNotNull())


def forecast_known_at(p_hours: int) -> Column:
    return (
        F.col("valid_time")
        - F.make_interval(hours=F.col("lead_days") * 24)
        + F.make_interval(hours=F.lit(p_hours))
    )


def landed_forecast(points: DataFrame, p_hours: int) -> DataFrame:
    """A lead-N value lands only when its known_at is at or before the fetch."""
    return points.withColumn("known_at", forecast_known_at(p_hours)).where(
        F.col("known_at") <= F.col("fetched_at")
    )


def keep_by_sequence(df: DataFrame, keys: list[str], sequence: Column) -> DataFrame:
    """What AUTO CDC SCD1 keeps: per key, the row with the largest sequence."""
    w = Window.partitionBy(*keys).orderBy(sequence.desc())
    return df.withColumn("_rn", F.row_number().over(w)).where("_rn = 1").drop("_rn")


FORECAST_KEY = ["cbsa_code", "valid_time", "lead_days"]


def first_sighting_sequence() -> Column:
    """AUTO CDC keeps the largest sequence; negated fetch time makes that the first sighting."""
    return -F.unix_timestamp("fetched_at")


def first_sightings(landed: DataFrame) -> DataFrame:
    return keep_by_sequence(landed, FORECAST_KEY, first_sighting_sequence())


FILL_RUN = 6
FILL_KEY = ["path", "cbsa_code", "lead_days"]


def _equal_runs(landed: DataFrame) -> DataFrame:
    """Each hour whose lead equals its envelope's lead 0, with the length of the run it sits in."""
    hour = F.unix_timestamp("valid_time") / 3600
    df = landed.select(*FILL_KEY, "valid_time", (F.col("temp_c") == F.col("lead0_c")).alias("eq"))
    w = Window.partitionBy(*FILL_KEY, "eq").orderBy("valid_time")
    df = df.withColumn("grp", hour - F.row_number().over(w)).where("eq")
    return df.withColumn("run", F.count("*").over(Window.partitionBy(*FILL_KEY, "grp")))


def fill_runs(landed: DataFrame) -> DataFrame:
    """Longest run of consecutive landed hours equal to the envelope's lead 0, per envelope, metro, lead."""
    best = _equal_runs(landed).groupBy(*FILL_KEY).agg(F.max("run").alias("max_run"))
    base = landed.select(*FILL_KEY).distinct()
    return base.join(best, FILL_KEY, "left").fillna({"max_run": 0})


def fill_quarantine(landed: DataFrame) -> DataFrame:
    """Hours in a run of FILL_RUN or more equal to lead 0: set aside with a reason, not trusted."""
    runs = _equal_runs(landed).where(F.col("run") >= FILL_RUN)
    reason = F.lit(f"lead_equals_lead0_{FILL_RUN}h")
    keep = landed.select(*FILL_KEY, "valid_time", "fetched_at", "known_at")
    out = runs.select(*FILL_KEY, "valid_time", "run", reason.alias("reason"))
    return out.join(keep, [*FILL_KEY, "valid_time"])


def fill_summary(quarantine: DataFrame) -> DataFrame:
    """Per envelope and lead, the metros and hours quarantined: one metro is weather, many a fill."""
    return quarantine.groupBy("path", "lead_days").agg(
        F.countDistinct("cbsa_code").alias("n_metros"),
        F.count("*").alias("hours"),
        F.max("fetched_at").alias("fetched_at"),
    )


def drop_quarantined(hourly: DataFrame, quarantine: DataFrame) -> DataFrame:
    return hourly.join(
        quarantine.select(*FILL_KEY, "valid_time"), [*FILL_KEY, "valid_time"], "left_anti"
    )


def _utc_day() -> Column:
    return F.date_from_unix_date(F.floor(F.unix_timestamp("valid_time") / 86400))


def quarantined_days(hourly: DataFrame, quarantine: DataFrame) -> DataFrame:
    """The metro, day and lead cells that lost hours to the quarantine, with their latest known_at."""
    gone = hourly.join(
        quarantine.select(*FILL_KEY, "valid_time"), [*FILL_KEY, "valid_time"], "left_semi"
    )
    return gone.groupBy("cbsa_code", _utc_day().alias("target_date"), "lead_days").agg(
        F.max("known_at").alias("known_at")
    )


def forecast_daily(hourly: DataFrame) -> DataFrame:
    g = hourly.groupBy("cbsa_code", _utc_day().alias("target_date"), "lead_days").agg(
        F.avg("temp_c").alias("temp_c"),
        F.count("*").alias("hours"),
        F.max("known_at").alias("known_at"),
    )
    return g.where("hours = 24").drop("hours")


def era5_days(env: DataFrame, metros: DataFrame) -> DataFrame:
    locs = env.select(
        "path", "fetched_at", "slot", F.posexplode(_locations(DAILY, "daily")).alias("pos", "loc")
    )
    d = F.col("loc.daily")
    days = locs.select(
        "path",
        "fetched_at",
        "slot",
        (F.col("pos") + 1).alias("rank"),
        F.posexplode(d["time"]).alias("i", "day"),
        d.alias("d"),
    )
    out = days.select(
        "path",
        "fetched_at",
        "slot",
        "rank",
        F.to_date("day").alias("date"),
        *[
            F.col("d")[c][F.col("i")].alias(c.replace("temperature_2m_", "t_"))
            for c in ("temperature_2m_mean", "temperature_2m_max", "temperature_2m_min")
        ],
    )
    return out.join(metros.select("rank", "cbsa_code"), "rank")


def utc_wall(text: Column, pattern: str = "yyyy-MM-dd'T'HH:mm") -> Column:
    """A zone-less wall time read as UTC; `to_timestamp` alone would read it in the session zone."""
    return F.to_timestamp(F.concat(text, F.lit("Z")), pattern + "X")


def sessions(seed: DataFrame) -> DataFrame:
    """The session calendar seed, its open and close read as the UTC instants they name."""
    secs = "yyyy-MM-dd'T'HH:mm:ss"
    return seed.select(
        F.to_date("session_date").alias("session_date"),
        utc_wall(F.col("open_utc"), secs).alias("open_utc"),
        utc_wall(F.col("close_utc"), secs).alias("close_utc"),
        F.to_date("end_date").alias("end_date"),
    )


def utc_midnight(day: Column, plus_days: int = 0) -> Column:
    """Midnight UTC `plus_days` after `day`; a date cast to timestamp would take the session zone."""
    return F.timestamp_seconds((F.unix_date(day) + plus_days) * 86400)


def era5_known(slot: Column, day: Column, fetched_at: Column) -> Column:
    """A backfill chunk (slot `a..b`) is known at date + 8 d; a steady envelope at its fetch."""
    backfill = utc_midnight(day, ERA5_BACKFILL_DELAY.days)
    return F.when(slot.contains(".."), backfill).otherwise(fetched_at)


def era5_first_seen(days: DataFrame) -> DataFrame:
    """Per date, the earliest envelope with all three variables for every metro, by `era5_known`."""
    complete = days.where("t_mean IS NOT NULL AND t_max IS NOT NULL AND t_min IS NOT NULL")
    per_env = complete.groupBy("path", "fetched_at", "slot", "date").agg(
        F.countDistinct("cbsa_code").alias("n")
    )
    known = era5_known(F.col("slot"), F.col("date"), F.col("fetched_at"))
    return per_env.where(F.col("n") == N_METROS).groupBy("date").agg(F.min(known).alias("known_at"))


def price_first_seen(prices_raw: DataFrame) -> DataFrame:
    return prices_raw.groupBy("source", "ticker", "trade_date").agg(
        F.min("fetched_at").alias("first_seen")
    )


def era5_bound_ok(date_col: str = "date") -> Column:
    return F.col("known_at") >= utc_midnight(F.col(date_col), 5)


def _per_source(prices_raw: DataFrame, sessions: DataFrame) -> DataFrame:
    """Each source's first-seen close per bar, and the time that source lends it."""
    first = prices_raw.groupBy("source", "ticker", "trade_date").agg(
        F.min_by("close", "fetched_at").alias("close"),
        F.min("fetched_at").alias("first_seen"),
        F.countDistinct("close").alias("closes"),
    )
    s = sessions.select(F.col("session_date").alias("trade_date"), "close_utc")
    return first.join(s, "trade_date", "left").withColumn(
        "src_known_at",
        F.when(
            F.col("source") == "yahoo", F.col("close_utc") + F.expr("INTERVAL 24 HOURS")
        ).otherwise(F.col("first_seen")),
    )


def _primary(first: DataFrame) -> DataFrame:
    prim = first.withColumn("pri", F.when(F.col("source") == "alphavantage", 0).otherwise(1))
    pw = Window.partitionBy("ticker", "trade_date").orderBy("pri")
    return (
        prim.withColumn("_rn", F.row_number().over(pw))
        .where("_rn = 1")
        .select(
            "ticker",
            "trade_date",
            F.col("close").alias("p_close"),
            F.col("closes").alias("p_closes"),
        )
    )


def prices_daily(prices_raw: DataFrame, sessions: DataFrame) -> DataFrame:
    """One row per bar: the primary source's first-seen close and the earliest agreeing source's time."""
    first = _per_source(prices_raw, sessions)
    j = first.join(_primary(first), ["ticker", "trade_date"])
    agree = F.abs(F.col("close") / F.col("p_close") - 1) <= AGREE_WITHIN
    out = j.groupBy("ticker", "trade_date").agg(
        F.first("p_close").alias("close"),
        F.min(F.when(agree, F.col("src_known_at"))).alias("known_at"),
        F.sum(F.when(~agree, 1).otherwise(0)).alias("disagreements"),
        (F.max("p_closes") - 1).alias("revisions"),
        F.first("close_utc").alias("close_utc"),
    )
    rw = Window.partitionBy("ticker").orderBy("trade_date")
    return out.withColumn("ret", F.col("close") / F.lag("close").over(rw) - 1)


def _weighted(df: DataFrame, metros: DataFrame, by: list[str], value: str) -> DataFrame:
    """Weighted mean over the metros present, with their count and summed weight."""
    j = df.join(metros.select("cbsa_code", "weight"), "cbsa_code")
    return j.groupBy(*by).agg(
        (F.sum(F.col(value) * F.col("weight")) / F.sum("weight")).alias("index_c"),
        F.countDistinct("cbsa_code").alias("n_metros"),
        F.sum("weight").alias("w_present"),
        *([F.max("known_at").alias("known_at")] if "known_at" in df.columns else []),
    )


def weighted(df: DataFrame, metros: DataFrame, by: list[str], value: str) -> DataFrame:
    """The weighted mean where every metro is present."""
    g = _weighted(df, metros, by, value)
    return g.where(F.col("n_metros") == N_METROS).drop("w_present")


def temp_index_daily(observed: DataFrame, metros: DataFrame) -> DataFrame:
    return weighted(observed, metros, ["date"], "t_mean")


def normals(index_daily: DataFrame, closes: DataFrame | None = None) -> DataFrame:
    """`index.normals` in Spark; `closes` is the primary's `trade_date`s. `period` names the window."""
    lo, hi = NORMALS_YEARS
    std = _standard_normals(index_daily)
    if closes is None:
        return std
    bounds = _training_half(index_daily, closes)
    years = index_daily.where(F.year("date").between(lo, hi)).agg(
        (F.countDistinct(F.year("date")) == hi - lo + 1).alias("complete")
    )
    has_half = bounds.agg((F.count("*") > 0).alias("has_half"))
    use_std = years.crossJoin(has_half).select(
        (F.col("complete") | ~F.col("has_half")).alias("use_std")
    )
    pick = std.crossJoin(use_std).where("use_std")
    alt = _half_normals(index_daily, bounds).crossJoin(use_std).where("NOT use_std")
    return pick.unionByName(alt).drop("use_std")


def _standard_normals(index_daily: DataFrame) -> DataFrame:
    lo, hi = NORMALS_YEARS
    expected = F.when(F.col("day_of_year") == 366, expected_years(366)).otherwise(expected_years(1))
    return (
        index_daily.where(F.year("date").between(lo, hi))
        .groupBy(F.dayofyear("date").alias("day_of_year"))
        .agg(F.avg("index_c").alias("mean_c"), F.countDistinct(F.year("date")).alias("n_years"))
        .select(
            "day_of_year",
            F.when(F.col("n_years") >= expected, F.col("mean_c")).alias("normal_c"),
            "n_years",
            F.lit(f"{lo}-{hi}").alias("period"),
        )
    )


def _training_half(index_daily: DataFrame, closes: DataFrame) -> DataFrame:
    """One row (`first`, `mid`) over the sessions with both an index and a close; none under two."""
    both = index_daily.select("date").join(
        closes.select(F.col("trade_date").alias("date")).distinct(), "date"
    )
    ranked = both.select(
        "date",
        (F.row_number().over(Window.orderBy("date")) - 1).alias("i"),
        F.count("*").over(Window.partitionBy()).alias("n"),
        F.min("date").over(Window.partitionBy()).alias("first"),
    )
    return ranked.where((F.col("n") >= 2) & (F.col("i") == F.floor(F.col("n") / 2))).select(
        "first", F.col("date").alias("mid")
    )


def _half_normals(index_daily: DataFrame, bounds: DataFrame) -> DataFrame:
    return (
        index_daily.crossJoin(bounds)
        .where((F.col("date") >= F.col("first")) & (F.col("date") < F.col("mid")))
        .groupBy(F.dayofyear("date").alias("day_of_year"), "first", "mid")
        .agg(F.avg("index_c").alias("normal_c"), F.countDistinct(F.year("date")).alias("n_years"))
        .select(
            "day_of_year",
            "normal_c",
            "n_years",
            F.concat(
                F.year("first").cast("string"),
                F.lit("-"),
                F.year(F.date_sub("mid", 1)).cast("string"),
                F.lit(" (training half)"),
            ).alias("period"),
        )
    )


def forecast_index(
    daily: DataFrame, metros: DataFrame, norms: DataFrame, quarantined: DataFrame
) -> DataFrame:
    """The index where the metros present carry MIN_FORECAST_WEIGHT of the weight. A cell the
    quarantine alone put under it is kept without a value, flagged `quarantined`."""
    by = ["target_date", "lead_days"]
    idx = _weighted(daily, metros, by, "temp_c")
    q = (
        quarantined.join(metros.select("cbsa_code", "weight"), "cbsa_code")
        .groupBy(*by)
        .agg(
            F.countDistinct("cbsa_code").alias("n_quarantined"),
            F.sum("weight").alias("w_quarantined"),
            F.max("known_at").alias("q_known_at"),
        )
    )
    total = metros.agg(F.sum("weight").alias("w_total"))
    out = idx.join(q, by, "full").crossJoin(total)
    share = F.coalesce("w_present", F.lit(0.0)) / F.col("w_total")
    q_share = F.coalesce("w_quarantined", F.lit(0.0)) / F.col("w_total")
    kept = share >= MIN_FORECAST_WEIGHT
    flagged = ~kept & (share + q_share >= MIN_FORECAST_WEIGHT)
    out = out.where(kept | flagged).select(
        *by,
        F.when(kept, F.col("index_c")).alias("index_c"),
        "n_metros",
        share.alias("w_share"),
        F.when(kept, F.col("known_at"))
        .otherwise(F.greatest("known_at", "q_known_at"))
        .alias("known_at"),
        F.coalesce("n_quarantined", F.lit(0)).alias("n_quarantined"),
        flagged.alias("quarantined"),
    )
    out = out.withColumn("day_of_year", F.dayofyear("target_date"))
    return (
        out.join(norms.select("day_of_year", "normal_c"), "day_of_year", "left")
        .withColumn("anomaly_c", F.col("index_c") - F.col("normal_c"))
        .drop("day_of_year")
    )


INTRADAY_COLUMNS = (
    "ticker", "bar_start", "open", "high", "low", "close", "volume", "source", "final", "fetched_at",
)  # fmt: skip


def prices_intraday(bars: DataFrame, source: str) -> DataFrame:
    """Display-only bars: each envelope replaces rows from its oldest bar on; the newest
    envelope's `keep_from` bounds retention. `bars` has path, fetched_at, keep_from and the bar."""
    keep = bars.agg(F.max_by("keep_from", "fetched_at").alias("keep"))
    b = bars.crossJoin(keep).where(F.col("fetched_at") >= F.col("keep"))
    first = b.groupBy("path").agg(
        F.min("bar_start").alias("l_first"), F.max("fetched_at").alias("l_at")
    )
    replaced = (F.col("l_at") > F.col("fetched_at")) & (F.col("l_first") <= F.col("bar_start"))
    kept = b.join(first, replaced, "left_anti").where(F.col("bar_start") >= F.col("keep"))
    return kept.withColumn("source", F.lit(source)).select(*INTRADAY_COLUMNS)
