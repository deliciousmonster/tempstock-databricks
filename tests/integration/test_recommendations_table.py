"""`signals.recommendations` (dev): the action comes from `signals.signal`; flips, NULLs and edits refused."""

from datetime import timedelta

from tests.integration.conftest import sql_error
from tests.integration.helpers import AGENT_MERGE, agent_args, frame, scratch_as_of
from weather_signal.jobs.score import write_signal


def _merge(spark, n, args):
    q = AGENT_MERGE.format(
        recs=n.table("signals", "recommendations"), signal=n.table("signals", "signal")
    )
    spark.sql(q, args=args).collect()


def _rows(spark, n, as_of):
    return spark.sql(
        f"SELECT * FROM {n.table('signals', 'recommendations')} "
        f"WHERE as_of = TIMESTAMP'{as_of.isoformat()}'"
    ).collect()


def test_action_from_signal_and_no_signal_no_row(spark, n, run_id):
    as_of = scratch_as_of(run_id)
    rows = frame(as_of)
    write_signal(spark, n, rows)
    _merge(spark, n, agent_args(as_of, rows[0]["target_date"]))
    got = _rows(spark, n, as_of)
    assert len(got) == 1 and got[0]["signal_action"] == rows[0]["action"]
    later = as_of + timedelta(minutes=1)
    _merge(spark, n, agent_args(later, rows[0]["target_date"]))
    assert _rows(spark, n, later) == []


def test_flip_null_update_delete_refused(spark, n, run_id):
    recs = n.table("signals", "recommendations")
    t = scratch_as_of(run_id).isoformat()
    cols = "(as_of, target_date, signal_action, action, confidence)"
    flip = sql_error(
        spark,
        f"INSERT INTO {recs} {cols} VALUES (TIMESTAMP'{t}', DATE'2001-01-01', 'buy', 'sell', 0.5)",
    )
    null = sql_error(
        spark,
        f"INSERT INTO {recs} {cols} VALUES (TIMESTAMP'{t}', DATE'2001-01-01', 'buy', NULL, 0.5)",
    )
    assert "never_flip" in flip and ("action_not_null" in null or "never_flip" in null)
    assert "DELTA_CANNOT_MODIFY_APPEND_ONLY" in sql_error(
        spark, f"UPDATE {recs} SET action = 'hold'"
    )
    assert "DELTA_CANNOT_MODIFY_APPEND_ONLY" in sql_error(spark, f"DELETE FROM {recs}")


def test_audit_view_lists_a_hand_inserted_row(spark, n, run_id):
    view, recs = n.table("signals", "v_recommendation_audit"), n.table("signals", "recommendations")
    t = (scratch_as_of(run_id) + timedelta(minutes=2)).isoformat()
    q = f"SELECT count(*) AS c FROM {view} WHERE as_of = TIMESTAMP'{t}'"
    assert spark.sql(q).first()["c"] == 0
    spark.sql(
        f"INSERT INTO {recs} (as_of, target_date, signal_action, action, confidence) "
        f"VALUES (TIMESTAMP'{t}', DATE'2001-01-01', 'hold', 'hold', 0.5)"
    ).collect()
    assert spark.sql(q).first()["c"] == 1
