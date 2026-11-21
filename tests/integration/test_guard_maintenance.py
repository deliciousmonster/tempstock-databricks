"""OPTIMIZE and VACUUM on a clustered `dev` copy leave the guard passing and its cursor moved."""

from datetime import UTC, datetime

from tests.integration.helpers import drop_scratch, scratch_spec
from weather_signal.catalog import setup
from weather_signal.guard import check


def test_optimize_and_vacuum_pass(spark, cat, n, run_id):
    spec = scratch_spec(n, "signal", run_id)
    try:
        setup.reconcile(cat, spec)
        now = datetime.now(UTC)
        check.run(cat, [spec], now, "pre", now, lambda _m: None)
        before = next(r for r in cat.guard_rows() if r.table_name == spec.name).checked_version
        for q in (
            f"INSERT INTO {spec.name} (as_of, target_date, horizon_days, action, confidence, "
            "scored_at) VALUES (TIMESTAMP'2001-01-01 00:30', DATE'2001-01-01', 0, 'hold', 0, now())",
            f"OPTIMIZE {spec.name}",
            f"VACUUM {spec.name}",
        ):
            spark.sql(q).collect()
        ops = {c.operation for c in cat.history(spec.name, before)}
        out = check.run(cat, [spec], now, "pre", now, lambda _m: None)
        assert check.failure_reason(out, None) is None, (ops, [o.detail for o in out])
        after = next(r for r in cat.guard_rows() if r.table_name == spec.name).checked_version
        assert after > before
        assert {"VACUUM START", "VACUUM END"} <= ops
    finally:
        drop_scratch(spark, n, spec.name)
