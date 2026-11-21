"""verify-agent's SQL, run on local Spark: the run log joined to the hour's signal rows, then that
run's recommendations and signal. Before this it ran only live."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

from tests.spark.conftest import ts
from weather_signal import config
from weather_signal.catalog import ddl
from weather_signal.deploy import proof_checks as pc
from weather_signal.deploy import proof_verify

SINCE = datetime(2026, 11, 19, 21, 0, tzinfo=UTC)


def _proof(spark, n):
    """A Proof as `_agent_sql` reads it: scope names and Connect's `sql`, rows as dicts."""
    sql = lambda q: [r.asDict() for r in spark.sql(q).collect()]  # noqa: E731
    return SimpleNamespace(ctx=SimpleNamespace(scope=SimpleNamespace(names=n), sql=sql))


def _table(spark, name, columns, rows):
    spark.sql(f"CREATE TABLE {name} ({columns.replace(' NOT NULL', '')}) USING parquet")
    cols = [c.split()[0] for c in columns.split(", ")]
    if rows:
        full = [tuple(r.get(c) for c in cols) for r in rows]
        spark.createDataFrame(full, spark.table(name).schema).write.insertInto(name)


def _world(spark, tmp_path):
    n = config.Names("spark_catalog", "t8_")
    spark.sql(f"CREATE DATABASE IF NOT EXISTS {n.schema('signals')} LOCATION '{tmp_path}'")
    h21, h22 = ts(2026, 11, 19, 21, 0), ts(2026, 11, 19, 22, 0)
    runs = [  # the first began before SINCE
        {"run_id": "0000aaaa", "as_of": h22, "started_at": ts(2026, 11, 19, 20, 40)},
        {"run_id": "1111bbbb", "as_of": h21, "started_at": ts(2026, 11, 19, 21, 40)},
        {"run_id": "2222cccc", "as_of": h22, "started_at": ts(2026, 11, 19, 22, 40)},
    ]
    outcomes = {"0000aaaa": "ok", "1111bbbb": "signal_missing", "2222cccc": "ok"}
    for r in runs:
        r.update(outcome=outcomes[r["run_id"]], route="direct")
    _table(spark, n.table("signals", "agent_runs"), ddl.AGENT_RUNS_COLUMNS, runs)
    calls = [(date(2026, 11, 20), "buy"), (date(2026, 11, 21), "sell")]
    signal = [{"as_of": h22, "target_date": d, "action": a} for d, a in calls]
    _table(spark, n.table("signals", "signal"), ddl.SIGNAL_COLUMNS, signal)
    recs = [
        {"as_of": h22, "target_date": calls[0][0], "signal_action": "buy", "action": "hold",
         "written_at": ts(2026, 11, 19, 20, 41)},  # the earlier run's, before 2222cccc began
        *({"as_of": h22, "target_date": s["target_date"], "signal_action": s["action"],
           "action": s["action"], "written_at": ts(2026, 11, 19, 22, 41)} for s in signal),
    ]  # fmt: skip
    _table(spark, n.table("signals", "recommendations"), ddl.RECOMMENDATION_COLUMNS, recs)
    return n


def test_has_signal_grades_the_first_run_since_whose_hour_has_signal_rows(spark, tmp_path):
    n = _world(spark, tmp_path)
    try:
        run, earlier, recs, signal = proof_verify._agent_sql(_proof(spark, n), SINCE)
        assert run is not None and run["run_id"] == "2222cccc" and run["has_signal"] is True
        assert len(earlier) == 1 and "1111bbbb" in earlier[0] and "no signal rows" in earlier[0]
        assert sorted((str(r["target_date"]), r["action"]) for r in recs) == [
            ("2026-11-20", "buy"),
            ("2026-11-21", "sell"),
        ]
        assert len(signal) == 2
        assert all(c.ok for c in pc.agent_checks(run, recs, signal))
    finally:
        spark.sql(f"DROP DATABASE IF EXISTS {n.schema('signals')} CASCADE")


def test_no_run_has_signal_when_the_hour_has_no_rows(spark, tmp_path):
    n = _world(spark, tmp_path)
    try:
        spark.sql(f"TRUNCATE TABLE {n.table('signals', 'signal')}")
        run, earlier, recs, signal = proof_verify._agent_sql(_proof(spark, n), SINCE)
        assert run is None and recs == [] and signal == []
        assert [e.split()[1] for e in earlier] == ["1111bbbb", "2222cccc"]
    finally:
        spark.sql(f"DROP DATABASE IF EXISTS {n.schema('signals')} CASCADE")
