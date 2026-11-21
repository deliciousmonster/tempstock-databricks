"""The baseline's backtest digest reads the newest run, so a migrated and a rebuilt scope agree."""

from __future__ import annotations

from datetime import date

from weather_signal.deploy import baseline


class Scope:
    def fq(self, schema: str, table: str) -> str:
        return f"{schema}__{table}"


def _table(spark, rows: str) -> None:
    spark.sql(
        "CREATE OR REPLACE TEMP VIEW signals__signal_backtest AS SELECT * FROM VALUES "
        f"{rows} AS t(backtest_run_id, created_at, target_date, action)"
    )


def _digest(spark) -> dict:
    def sql(q):
        return [r.asDict() for r in spark.sql(q).collect()]

    return baseline.backtest_calls(sql, Scope(), date(2024, 12, 31))


V2 = (
    "('h0:threshold_wf:2', TIMESTAMP'2026-11-21 00:00:00', DATE'2024-03-04', 'hold'), "
    "('h0:threshold_wf:2', TIMESTAMP'2026-11-21 00:00:00', DATE'2024-03-05', 'hold')"
)
V1 = (
    "('h0:threshold_wf:1', TIMESTAMP'2026-11-18 00:00:00', DATE'2024-03-04', 'buy'), "
    "('h0:threshold_wf:1', TIMESTAMP'2026-11-18 00:00:00', DATE'2024-03-05', 'hold')"
)


def test_an_earlier_run_beside_the_newest_leaves_the_digest_unchanged(spark):
    _table(spark, V2)
    rebuilt = _digest(spark)
    _table(spark, f"{V1}, {V2}")
    migrated = _digest(spark)
    assert migrated == rebuilt and rebuilt["rows"] == 2


def test_the_digest_of_many_rows_does_not_overflow(spark):
    spark.sql(
        "CREATE OR REPLACE TEMP VIEW signals__signal_backtest AS SELECT 'h0:m:1' backtest_run_id, "
        "TIMESTAMP'2026-11-21 00:00:00' created_at, date_add(DATE'2000-01-01', CAST(id AS INT)) "
        "target_date, 'buy' action FROM range(2000)"
    )
    assert _digest(spark)["rows"] == 2000
