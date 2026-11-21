"""`setup` is a guard act: rerunnable, and a stop between its commits is finished or caught, never lost."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from tests.integration.helpers import drop_scratch, scratch_spec
from weather_signal.catalog import setup
from weather_signal.guard import check


class Stop(Exception):
    pass


class StopAfter:
    """The catalog, stopping right after the first statement that starts with `prefix`."""

    def __init__(self, cat, prefix):
        self._cat, self._prefix, self.fired = cat, prefix, False

    def __getattr__(self, name):
        return getattr(self._cat, name)

    def execute(self, sql):
        self._cat.execute(sql)
        if not self.fired and sql.startswith(self._prefix):
            self.fired = True
            raise Stop(sql[:60])


@pytest.fixture
def spec(spark, n, run_id):
    s = scratch_spec(n, "signal", run_id)
    yield s
    drop_scratch(spark, n, s.name)


def _guard(cat, spec):
    now = datetime.now(UTC)
    return check.run(cat, [spec], now, "pre", now, lambda _m: None)


def test_setup_twice(cat, spec):
    a = setup.reconcile(cat, spec)
    assert setup.reconcile(cat, spec) == a
    assert check.failure_reason(_guard(cat, spec), None) is None


def test_stop_after_create_then_rerun(cat, spec):
    stopping = StopAfter(cat, "CREATE TABLE")
    with pytest.raises(Stop):
        setup.reconcile(stopping, spec)
    assert check.failure_reason(_guard(cat, spec), None) == "guard_incident"
    setup.reconcile(cat, spec)
    assert check.failure_reason(_guard(cat, spec), None) is None


def test_stop_after_alter_is_matched_by_check(cat, spec, spark, n):
    # An older spec without one constraint, so the full spec's reconcile has an ALTER to make.
    setup.reconcile(
        cat, replace(spec, constraints={"action_valid": spec.constraints["action_valid"]})
    )
    stopping = StopAfter(cat, "ALTER TABLE")
    with pytest.raises(Stop):
        setup.reconcile(stopping, spec)
    before = spark.sql(
        f"SELECT count(*) AS c FROM {n.schema('ops')}.guard_incidents "
        f"WHERE table_name = '{spec.name}'"
    ).first()["c"]
    out = _guard(cat, spec)
    after = spark.sql(
        f"SELECT count(*) AS c FROM {n.schema('ops')}.guard_incidents "
        f"WHERE table_name = '{spec.name}'"
    ).first()["c"]
    assert after == before
    assert check.failure_reason(out, None) is None, [o.detail for o in out]
