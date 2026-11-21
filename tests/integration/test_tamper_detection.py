"""Tamper and replacement on `dev` scratch copies of `signals.recommendations`, run as the owner.

Not covered here (stated in the design note): running as the runner principal, and the agent's
refusal on `post_verified_as_of` with `check` made unable to read.
"""

from datetime import UTC, datetime

import pytest

from tests.integration.helpers import drop_scratch, scratch_spec
from weather_signal.catalog import setup
from weather_signal.guard import check, repair

COLS = "(as_of, target_date, signal_action, action, confidence)"


@pytest.fixture
def spec(spark, n, run_id):
    s = scratch_spec(n, "recommendations", run_id)
    yield s
    for name in (
        s.name,
        *(
            r["tableName"]
            for r in spark.sql(f"SHOW TABLES IN {n.schema('signals')} LIKE '*{run_id}_*'").collect()
        ),
    ):
        drop_scratch(spark, n, name if "." in name else f"{n.schema('signals')}.{name}")


def _guard(cat, spec, sent):
    now = datetime.now(UTC)
    return check.run(cat, [spec], now, "pre", now, sent.append)


def _incidents(spark, n, spec):
    return spark.sql(
        f"SELECT * FROM {n.schema('ops')}.guard_incidents WHERE table_name = '{spec.name}'"
    ).collect()


def test_case_a_tamper_restore(spark, cat, n, spec):
    setup.reconcile(cat, spec)
    spark.sql(
        f"INSERT INTO {spec.name} {COLS} VALUES "
        "(TIMESTAMP'2001-01-01 00:30', DATE'2001-01-02', 'buy', 'buy', 0.5)"
    ).collect()
    sent: list[str] = []
    assert check.failure_reason(_guard(cat, spec, sent), None) is None
    for q in (
        f"ALTER TABLE {spec.name} UNSET TBLPROPERTIES ('delta.appendOnly', 'delta.enableChangeDataFeed')",
        f"ALTER TABLE {spec.name} DROP CONSTRAINT never_flip",
        f"UPDATE {spec.name} SET action = 'sell'",
        f"ALTER TABLE {spec.name} SET TBLPROPERTIES ('delta.appendOnly' = 'true', "
        "'delta.enableChangeDataFeed' = 'true')",
    ):
        spark.sql(q).collect()
    out = _guard(cat, spec, sent)
    assert check.failure_reason(out, None) == "guard_incident"
    rows = _incidents(spark, n, spec)
    assert len(rows) == 1 and rows[0]["first_bad_version"] is not None
    assert "buy" in (rows[0]["removed_since_good"] or "")  # preimage from the snapshot diff
    assert cat.current_user() in rows[0]["commits"]
    assert not check.may_write(
        [r for r in cat.guard_rows() if r.table_name == spec.name], datetime.now(UTC), "pre"
    )
    n_sent = len(sent)
    _guard(cat, spec, sent)
    assert len(_incidents(spark, n, spec)) == 1 and len(sent) == n_sent
    spark.sql(
        f"INSERT INTO {spec.name} {COLS} VALUES "
        "(TIMESTAMP'2001-01-01 01:30', DATE'2001-01-02', 'hold', 'hold', 0.4)"
    ).collect()
    repair.repair(cat, spec, datetime.now(UTC))
    got = {(r["as_of"].astimezone(UTC).hour, r["action"]) for r in spark.table(spec.name).collect()}
    assert got == {(0, "buy"), (1, "hold")}
    assert check.failure_reason(_guard(cat, spec, sent), None) is None
    assert "never_flip" in str(cat.properties(spec.name))


def test_case_b_replaced_then_undropped(spark, cat, n, spec):
    tid = setup.reconcile(cat, spec)
    spark.sql(
        f"INSERT INTO {spec.name} {COLS} VALUES "
        "(TIMESTAMP'2001-01-01 00:30', DATE'2001-01-02', 'buy', 'buy', 0.5)"
    ).collect()
    sent: list[str] = []
    _guard(cat, spec, sent)
    spark.sql(f"DROP TABLE {spec.name}").collect()
    spark.sql(spec.create_sql()).collect()
    for name, expr in spec.constraints.items():
        spark.sql(f"ALTER TABLE {spec.name} ADD CONSTRAINT {name} CHECK ({expr})").collect()
    out = _guard(cat, spec, sent)
    assert check.failure_reason(out, None) == "guard_incident"
    assert _incidents(spark, n, spec)[-1]["kind"] == "replaced"
    repair.repair(cat, spec, datetime.now(UTC))
    assert cat.table_id(spec.name) == tid
    assert spark.table(spec.name).count() == 1
    assert check.failure_reason(_guard(cat, spec, sent), None) is None
