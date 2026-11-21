"""`dev.signals.agent_runs`: a second MERGE of a run id is a no-op; unknown outcomes and routes are refused; no UPDATE.

The CHECK legs fail on a table without change 10's constraints; the other columns are valid, so
a NOT NULL refusal can't stand in for the CHECK.
"""

import uuid

import pytest

from tests.integration.conftest import sql_error
from weather_signal.agent.store import SparkStore


def test_merge_once(spark, n):
    store = SparkStore(spark, n)
    rid = f"it-{uuid.uuid4().hex}"
    row = {
        "run_id": rid,
        "as_of": "2014-01-01T00:30:00",
        "outcome": "ok",
        "route": "databricks-gateway",
        "rows_written": 7,
    }
    store.insert_run(row)
    store.insert_run({**row, "outcome": "crashed"})
    got = spark.sql(
        f"SELECT outcome FROM {n.table('signals', 'agent_runs')} WHERE run_id = :r", args={"r": rid}
    ).collect()
    assert [g["outcome"] for g in got] == ["ok"]


@pytest.mark.parametrize(("col", "value"), [("outcome", "exploded"), ("route", "gateway")])
def test_unknown_values_refused(spark, n, col, value):
    t = n.table("signals", "agent_runs")
    vals = {"outcome": "ok", "route": "none", col: value}
    err = sql_error(
        spark,
        f"INSERT INTO {t} (run_id, as_of, outcome, route) VALUES ('it-{uuid.uuid4().hex}', "
        f"TIMESTAMP'2014-01-01 00:30:00', '{vals['outcome']}', '{vals['route']}')",
    )
    assert "CHECK_CONSTRAINT" in err.upper() or "CHECK CONSTRAINT" in err.upper()


def test_update_refused(spark, n):
    assert "DELTA_CANNOT_MODIFY_APPEND_ONLY" in sql_error(
        spark, f"UPDATE {n.table('signals', 'agent_runs')} SET outcome = 'ok'"
    )
