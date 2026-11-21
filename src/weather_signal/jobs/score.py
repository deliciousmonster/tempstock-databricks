"""The score job's tasks `guard`, `score` and `check`: wheel entry points."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from weather_signal import asof, config
from weather_signal.catalog.ddl import specs
from weather_signal.guard import check
from weather_signal.guard.catalog import Catalog
from weather_signal.guard.spark_catalog import SparkCatalog
from weather_signal.jobs.task import TaskFailed, task_args
from weather_signal.sessions import load as load_calendar
from weather_signal.signal import checks, score
from weather_signal.signal.champion import NoChampion, from_tags
from weather_signal.signal.features import Tables
from weather_signal.signal.model import BaseSignalModel
from weather_signal.spark import spark_session

log = logging.getLogger(__name__)
PRICE_DAYS = 40
MISSING = "RESOURCE_DOES_NOT_EXIST"


def _as_of(a: argparse.Namespace, now: datetime) -> datetime:
    # Every task of one run scores the same as_of, however long `refresh` takes.
    return asof.live_as_of(datetime.fromisoformat(a.as_of) if a.as_of else now)


def _notify(msg: str) -> None:
    # The job's failure notification carries the raised reason; this is the log line behind it.
    log.warning("%s", msg)


def guard_phase(
    cat: Catalog,
    n: config.Names,
    as_of: datetime,
    phase: check.Phase,
    now: datetime,
    notify: Callable[[str], None] = _notify,
) -> list[check.Outcome]:
    out = check.run(cat, specs(n).values(), as_of, phase, now, notify)
    reason = check.failure_reason(out, None)
    if reason:
        raise TaskFailed(
            f"{reason}: "
            + "; ".join(
                f"{o.table} {o.kind} {o.detail}" for o in out if o.kind in ("incident", "unknown")
            )
        )
    return out


def champion(n: config.Names, client: Any = None) -> BaseSignalModel:
    """`@champion` once per run. Absent holds with `no_champion`, logged; any other answer fails."""
    name = f"{n.schema('signals')}.{config.MODEL_NAME}"
    try:
        if client is None:
            import mlflow

            mlflow.set_tracking_uri("databricks")
            mlflow.set_registry_uri("databricks-uc")
            client = mlflow.MlflowClient()
        mv = client.get_model_version_by_alias(name, "champion")
    except Exception as err:
        if getattr(err, "error_code", None) == MISSING:
            log.warning("no champion for %s; every target holds with no_champion", name)
            return NoChampion()
        raise TaskFailed(f"no_champion: registry unreadable ({type(err).__name__})") from err
    return from_tags(dict(mv.tags), name, str(mv.version))


def load_tables(spark: Any, n: config.Names, as_of: datetime) -> Tables:
    signals, daily = n.schema("signals"), n.schema("daily")
    since = (as_of - timedelta(days=PRICE_DAYS)).date().isoformat()
    fc = spark.sql(
        f"SELECT target_date, lead_days, anomaly_c, known_at, quarantined FROM {signals}.forecast_index "
        f"WHERE target_date >= DATE'{as_of.date().isoformat()}'"
    ).toPandas()
    px = spark.sql(
        f"SELECT ticker, trade_date, close, known_at FROM {daily}.prices_daily "
        f"WHERE trade_date >= DATE'{since}'"
    ).toPandas()
    return Tables(fc, px)


def write_signal(spark: Any, n: config.Names, rows: list[dict[str, Any]]) -> None:
    from pyspark.sql import Row

    score.assert_unique(rows)
    table = n.table("signals", "signal")
    df = spark.createDataFrame([Row(**r) for r in rows], schema=spark.table(table).schema)
    view = f"signal_frame_{rows[0]['as_of']:%Y%m%d%H%M}"
    df.createOrReplaceTempView(view)
    spark.sql(score.merge_sql(table, view))


def score_task(
    spark: Any, cat: Catalog, n: config.Names, as_of: datetime, p_hours: int, now: datetime
) -> int:
    if not check.may_write(cat.guard_rows(), as_of, "pre"):
        raise TaskFailed("guard_incident: ops.guard_state is open or not verified for this as_of")
    rows = score.frame(
        as_of, p_hours, load_tables(spark, n, as_of), load_calendar(), champion(n), now
    )
    if not rows:
        raise TaskFailed("no_signal_rows: the frame is empty")
    write_signal(spark, n, rows)
    return len(rows)


def metrics(spark: Any, n: config.Names, today: date) -> list[checks.Finding]:
    daily = n.schema("daily")
    cal = load_calendar()
    out = checks.calendar_end(today, cal.end)
    d = spark.sql(
        f"SELECT coalesce(sum(disagreements), 0) AS n FROM (SELECT disagreements FROM "
        f"{daily}.prices_daily WHERE ticker = '{config.PRIMARY}' ORDER BY trade_date DESC LIMIT 30)"
    )
    out += checks.disagreements(int(d.first()["n"]))
    return out + fill_findings(spark, n, datetime.now(UTC))


def fill_findings(spark: Any, n: config.Names, now: datetime) -> list[checks.Finding]:
    """`forecast_filled` once per envelope: the envelopes reported are kept in `ops.fill_alerts`."""
    alerts = n.table("ops", "fill_alerts")
    spark.sql(f"CREATE TABLE IF NOT EXISTS {alerts} (path STRING, alerted_at TIMESTAMP)")
    seen = {r["path"] for r in spark.sql(f"SELECT path FROM {alerts}").collect()}
    # Epoch seconds: a Connect client's collect() gives naive local time, not UTC.
    s = spark.sql(
        "SELECT path, lead_days, n_metros, hours, unix_timestamp(fetched_at) AS fetched_s "
        f"FROM {n.schema('daily')}.forecast_fill_summary"
    ).collect()
    rows = [r.asDict() | {"fetched_at": datetime.fromtimestamp(r["fetched_s"], UTC)} for r in s]
    new = sorted({r["path"] for r in rows if checks.forecast_filled([r], now, seen)})
    if new:
        df = spark.createDataFrame([(p, now) for p in new], "path string, alerted_at timestamp")
        df.write.mode("append").saveAsTable(alerts)
    return checks.forecast_filled(rows, now, seen)


def _main(task: str, argv: Sequence[str] | None) -> int:
    a = task_args(f"weather-signal-{task}", argv)
    logging.basicConfig(level=logging.INFO)
    n, now = config.Names(a.catalog, a.schema_prefix), datetime.now(UTC)
    as_of = _as_of(a, now)
    spark = spark_session(a.profile)
    cat = SparkCatalog(spark, n)
    if task == "guard":
        guard_phase(cat, n, as_of, "pre", now)
    elif task == "score":
        log.info("scored %d rows for %s", score_task(spark, cat, n, as_of, a.p_hours, now), as_of)
    else:
        guard_phase(cat, n, as_of, "post", now)
        findings = metrics(spark, n, now.date())
        for f in findings:
            log.info("check %s %s: %s", f.level, f.name, f.detail)
        if checks.verdict(findings) == "fail":
            raise TaskFailed("check: " + "; ".join(f.name for f in findings if f.level == "fail"))
    return 0


def guard_main(argv: Sequence[str] | None = None) -> int:
    return _main("guard", argv)


def score_main(argv: Sequence[str] | None = None) -> int:
    return _main("score", argv)


def check_main(argv: Sequence[str] | None = None) -> int:
    return _main("check", argv)
