"""Repair keeps a recommendation written before `llm_route` existed (change 12) and drops a bad route."""

from __future__ import annotations

from weather_signal.catalog.ddl import specs
from weather_signal.config import Names
from weather_signal.guard.spark_catalog import SparkCatalog

N = Names("workspace", "")
RECS = N.table("signals", "recommendations")
COLS = (
    "CAST('2014-01-01 00:30:00' AS TIMESTAMP) as_of, CAST('2014-01-0{d}' AS DATE) target_date, "
    "0 horizon_days, 'hold' signal_action, 'hold' action, 0.5 confidence, 'r' rationale, "
    "CAST(NULL AS STRING) downgrade_reason, 'm' llm_model_id, 'u' signal_model_uri, 't' trace_id, "
    "'v' agent_version, CAST(NULL AS TIMESTAMP) written_at, {route} llm_route"
)


def test_null_route_kept_unknown_route_dropped(spark):
    spark.sql(f"SELECT {COLS.format(d=1, route="'none'")} WHERE false").createOrReplaceTempView(
        "base"
    )
    rows = [COLS.format(d=2, route="CAST(NULL AS STRING)"), COLS.format(d=3, route="'gateway'")]
    spark.sql(" UNION ALL ".join(f"SELECT {r}" for r in rows)).createOrReplaceTempView("src")
    kept, dropped = SparkCatalog(spark, N).valid_new_rows("base", "src", None, specs(N)[RECS])
    assert [r["llm_route"] for r in kept] == [None]
    assert [r["llm_route"] for r in dropped] == ["gateway"]
