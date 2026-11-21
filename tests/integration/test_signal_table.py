"""`signals.signal` (dev): a second score for one `as_of` adds nothing; UPDATE and DELETE are refused."""

from tests.integration.conftest import sql_error
from tests.integration.helpers import frame, scratch_as_of
from weather_signal.jobs.score import write_signal


def _count(spark, table, as_of):
    return spark.sql(
        f"SELECT count(*) AS c FROM {table} WHERE as_of = TIMESTAMP'{as_of.isoformat()}'"
    ).first()["c"]


def test_second_score_is_a_noop(spark, n, run_id):
    table, as_of = n.table("signals", "signal"), scratch_as_of(run_id)
    rows = frame(as_of)
    write_signal(spark, n, rows)
    first = _count(spark, table, as_of)
    write_signal(spark, n, rows)
    assert first == len(rows) == 7 and _count(spark, table, as_of) == first


def test_update_and_delete_refused(spark, n):
    table = n.table("signals", "signal")
    assert "DELTA_CANNOT_MODIFY_APPEND_ONLY" in sql_error(spark, f"UPDATE {table} SET score = 0")
    assert "DELTA_CANNOT_MODIFY_APPEND_ONLY" in sql_error(spark, f"DELETE FROM {table}")
