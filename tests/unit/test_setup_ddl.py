"""`setup` reconciles each job-owned table as one guard act."""

import json

import pytest

from tests.unit.fake_catalog import CHECKS, FakeCatalog
from tests.unit.guard_world import RECS, SIG, SPECS, N, world
from weather_signal.agent import runlog
from weather_signal.agent.route import ROUTES
from weather_signal.agent.store import project
from weather_signal.catalog import ddl, setup
from weather_signal.catalog.ddl import REQUIRED_PROPERTIES, state_problems
from weather_signal.guard.repair import RepairRefused


def _ops(cat, table):
    return [c.operation for c in cat.history(table, after=-1)]


def test_fresh_setup_creates_with_constraints_by_alter_and_records_id():
    cat = world()
    assert _ops(cat, RECS) == ["CREATE TABLE"] + ["ADD CONSTRAINT"] * 4
    props = cat.properties(RECS)
    for k in REQUIRED_PROPERTIES:
        assert k in props
    assert props["delta.deletedFileRetentionDuration"] == "interval 30 days"
    assert cat.guard[RECS].table_id == cat.table_id(RECS)
    assert cat.guard[SIG].checked_version == cat.latest_version(SIG)
    assert not any("CHECK" in s for s in cat.log if s.startswith("CREATE TABLE"))


def test_act_row_precedes_the_first_commit_with_planned_parameters():
    cat = world()
    first_act = cat.log.index("ACT pending " + RECS)
    first_commit = next(
        i for i, s in enumerate(cat.log) if s.startswith(f"CREATE TABLE IF NOT EXISTS {RECS}")
    )
    assert first_act < first_commit
    act = cat.acts(RECS)[0]
    assert act.status == "complete"
    adds = [p.parameters for p in act.planned if p.operation == "ADD CONSTRAINT"]
    assert {"name": "never_flip", "expr": "action IN (signal_action, 'hold')"} in adds


def _existing(constraints, props_extra=None):
    cat = FakeCatalog()
    for name, spec in SPECS.items():
        cat.create(name, {"delta.appendOnly": "true", **(props_extra or {})})
        for c, e in (constraints if name == RECS else spec.constraints).items():
            cat.execute(f"ALTER TABLE {name} ADD CONSTRAINT {c} CHECK ({e})")
    return cat


def test_reconcile_adds_missing_replaces_changed_leaves_matching():
    cat = _existing({"never_flip": "1=1", "action_not_null": "action  IS NOT NULL"})
    before = cat.latest_version(RECS)
    setup.run(cat, N)
    new = [(c.operation, c.parameters) for c in cat.history(RECS, after=before)]
    assert new[0] == ("DROP CONSTRAINT", {"constraint": "never_flip"})
    assert new[1] == (
        "ADD CONSTRAINT",
        {"name": "never_flip", "expr": "action IN (signal_action, 'hold')"},
    )
    assert new[2] == (
        "ADD CONSTRAINT",
        {"name": "confidence_not_null", "expr": "confidence IS NOT NULL"},
    )
    assert new[3] == (
        "ADD CONSTRAINT",
        {
            "name": "llm_route_valid",
            "expr": "llm_route IS NULL OR llm_route IN ('databricks-gateway', 'direct', 'direct_fallback', 'none')",
        },
    )
    assert not any(p.get("name") == "action_not_null" for _, p in new)
    set_props = json.loads(new[4][1]["properties"])
    assert set_props["delta.enableChangeDataFeed"] == "true"
    assert set_props["delta.logRetentionDuration"] == "interval 30 days"
    assert set_props["delta.deletedFileRetentionDuration"] == "interval 30 days"
    assert "delta.appendOnly" not in set_props


def test_short_retention_is_raised():
    cat = _existing(
        SPECS[RECS].constraints,
        {
            "delta.logRetentionDuration": "interval 7 days",
            "delta.deletedFileRetentionDuration": "interval 30 days",
            "delta.enableChangeDataFeed": "true",
        },
    )
    setup.run(cat, N)
    assert cat.properties(RECS)["delta.logRetentionDuration"] == "interval 30 days"


def test_second_run_changes_nothing():
    cat = world()
    v = {t: cat.latest_version(t) for t in (SIG, RECS)}
    setup.run(cat, N)
    assert {t: cat.latest_version(t) for t in (SIG, RECS)} == v


def test_refuses_with_open_incident():
    cat = world()
    cat.guard[SIG].open_incident = True
    v = cat.latest_version(SIG)
    with pytest.raises(RepairRefused):
        setup.reconcile(cat, SPECS[SIG])
    assert cat.latest_version(SIG) == v


def test_agent_runs_created_by_setup():
    cat = world()
    assert N.table("signals", "agent_runs") in cat.tables


RUNS = N.table("signals", "agent_runs")


def test_agent_runs_checks_outcome_and_route():
    """Change 10: the run log refuses an outcome or a route the agent cannot write."""
    props = world().properties(RUNS)
    outcome = props["delta.constraints.outcome_valid"]
    route = props["delta.constraints.route_valid"]
    for o in runlog.OUTCOMES:
        assert f"'{o}'" in outcome
    assert "'exploded'" not in outcome
    assert route == "route IN ('databricks-gateway', 'direct', 'direct_fallback', 'none')"
    assert set(ROUTES) == {"databricks-gateway", "direct", "direct_fallback", "none"}
    assert props["delta.constraints.fallback_has_reason"] == (
        "route <> 'direct_fallback' OR fallback_reason IS NOT NULL"
    )


def test_agent_runs_rerun_adds_no_constraint_twice():
    cat = world()
    v = cat.latest_version(RUNS)
    setup.run(cat, N)
    assert cat.latest_version(RUNS) == v


def test_agent_runs_has_a_column_for_every_run_field():
    """Change 10: what the worker writes lands in columns, so `detail` holds nothing it names."""
    spec = ddl.agent_runs(N)
    cols = {c.split()[0] for c in spec.columns.split(", ")}
    row = {
        **{k: 1 for k in ("rows_written", "downgrades", "hook_denials", "num_turns")},
        **{k: "x" for k in ("run_id", "as_of", "started_at", "finished_at", "outcome", "route")},
        **{k: "x" for k in ("fallback_reason", "databricks_service", "refusal", "error", "writer")},
        **{
            k: "x"
            for k in ("backend", "cli_version", "api_provider", "subscription", "sdk_version")
        },
        **{k: "x" for k in ("llm_model_id", "prompt_sha", "agent_version", "trace_id")},
        **{k: 1 for k in ("api_error_status", "duration_ms", "duration_api_ms", "cost_usd_equiv")},
    }
    assert "detail" not in project(row, cols)
    assert project(row, cols)["trace_id"] == "x"


def test_recommendations_carry_llm_route():
    """Change 12: the route is a column with its CHECK, and the agent binds it."""
    spec = SPECS[RECS]
    assert "llm_route STRING" in spec.columns
    expr = spec.constraints["llm_route_valid"]
    assert expr.startswith("llm_route IS NULL OR llm_route IN (")


def test_repair_keeps_a_row_from_before_llm_route():
    """The guard's repair counts a NULL CHECK as false, so a NULL route must pass by name."""
    row = {"as_of": 1, "target_date": 2, "action": "hold", "signal_action": "hold"}
    row["confidence"] = 0.5
    assert all(CHECKS[c](row) for c in SPECS[RECS].constraints)
    assert not CHECKS["llm_route_valid"]({**row, "llm_route": "gateway"})


def test_append_only_is_required_by_name():
    spec = SPECS[SIG]
    assert "'delta.appendOnly' = 'true'" in spec.create_sql()
    cat = world()
    props = {k: v for k, v in cat.properties(SIG).items() if k != "delta.appendOnly"}
    assert any("appendOnly" in p for p in state_problems(spec, props, 0))


def test_rerun_after_a_drop_records_an_incident_and_creates_nothing():
    """A dropped guarded table is a replacement, never a fresh baseline."""
    cat = world()
    original = cat.table_id(RECS)
    cat.drop(RECS)
    with pytest.raises(RepairRefused, match="replaced"):
        setup.run(cat, N)
    assert RECS not in cat.tables
    row = cat.guard[RECS]
    assert row.open_incident and row.table_id == original
    assert [i.kind for i in cat.incidents(original)] == ["replaced"]


def test_rerun_after_drop_and_recreate_keeps_the_old_id():
    cat = world()
    original = cat.table_id(RECS)
    cat.drop(RECS)
    cat.create(RECS, {})
    with pytest.raises(RepairRefused):
        setup.reconcile(cat, SPECS[RECS])
    assert cat.guard[RECS].table_id == original and cat.guard[RECS].open_incident


def _names(columns: str) -> list[str]:
    return [c.split()[0] for c in columns.split(", ")]


def test_an_older_signal_table_gets_the_estimate_columns_as_a_guard_act():
    cat = world()
    cat.tables[SIG].columns = _names(ddl.SCORED_COLUMNS)
    before = cat.latest_version(SIG)
    setup.run(cat, N)
    (c,) = cat.history(SIG, after=before)
    assert c.operation == "ADD COLUMNS"
    assert cat.columns(SIG)[-3:] == ["est_price", "est_reason", "est_basis"]
    act = cat.acts(SIG)[-1]
    assert act.status == "complete" and act.observed == [c.version]


def test_an_older_backtest_table_gets_the_pnl_and_gate_columns_at_the_end():
    cat = world()
    bt = N.table("signals", "signal_backtest")
    old = _names(ddl.BACKTEST_COLUMNS)[:16]
    cat.tables[bt].columns = list(old)
    setup.run(cat, N)
    assert cat.columns(bt) == _names(ddl.BACKTEST_COLUMNS)
    assert cat.columns(bt)[16:19] == ["pos", "ret", "pnl"]


def test_a_current_table_gets_no_alter():
    assert ddl.add_columns_sql("t", ddl.SIGNAL_COLUMNS, _names(ddl.SIGNAL_COLUMNS)) is None


def test_setup_creates_the_backtest_view_after_its_table():
    cat = world()
    log = [q.split(" (")[0].split(" AS ")[0] for q in cat.log]
    table = log.index(f"CREATE TABLE IF NOT EXISTS {N.table('signals', 'signal_backtest')}")
    assert log.index(f"CREATE OR REPLACE VIEW {N.schema('app')}.v_ui_backtest") > table
