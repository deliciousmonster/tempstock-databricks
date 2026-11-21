"""The guard's identity, state and history checks over a fake catalog."""

import json
import re

import pytest

from tests.conftest import FIXTURES, utc
from tests.unit.guard_world import RECS, SIG, SPECS, signal_row, world
from weather_signal.catalog.ddl import Planned
from weather_signal.guard import check
from weather_signal.guard.acts import perform

AS_OF, NOW = utc(2026, 12, 2, 12, 30), utc(2026, 12, 2, 12, 31)


def _guard(cat, table=SIG, as_of=AS_OF, phase="pre"):
    notes = []
    out = {o.table: o for o in check.run(cat, SPECS.values(), as_of, phase, NOW, notes.append)}
    return out[table], notes


def _passes(cat, table=SIG):
    o, _ = _guard(cat, table)
    assert o.kind in ("pass", "warn"), o.detail
    assert cat.guard[table].checked_version == cat.latest_version(table)
    return o


def _fails(cat, table=SIG):
    before = cat.guard[table].checked_version
    o, notes = _guard(cat, table)
    assert o.kind == "incident" and notes
    assert cat.guard[table].checked_version == before
    assert cat.guard[table].open_incident
    return o


def test_recorded_maintenance_history_passes():
    ops = re.findall(
        r"^v \d+ (.+)$", (FIXTURES / "delta-history-maintenance.out").read_text(), re.M
    )[::-1]
    assert ops[:2] == ["CREATE TABLE", "ADD CONSTRAINT"]
    cat = world()
    cat.commit(SIG, "WRITE", rows=[signal_row(1)])
    for op in ops[3:]:
        params = {"properties": '{"clusteringColumns":"[]"}'} if op == "LIQUID TAGGING" else {}
        cat.commit(SIG, op, params, user="predictive-optimization")
    assert _passes(cat).kind == "pass"  # allowlisted, not an unknown operation's warning


@pytest.mark.parametrize(
    "plan",
    [
        [
            Planned(
                "DROP CONSTRAINT",
                {"constraint": "action_valid"},
                f"ALTER TABLE {SIG} DROP CONSTRAINT action_valid",
            ),
            Planned(
                "ADD CONSTRAINT",
                {"name": "action_valid", "expr": "action IN ('buy', 'sell', 'hold')"},
                f"ALTER TABLE {SIG} ADD CONSTRAINT action_valid CHECK (action IN ('buy', 'sell', 'hold'))",
            ),
        ],
        [
            Planned(
                "SET TBLPROPERTIES",
                {"properties": '{"delta.logRetentionDuration":"interval 60 days"}'},
                f"ALTER TABLE {SIG} SET TBLPROPERTIES ('delta.logRetentionDuration' = 'interval 60 days')",
            )
        ],
    ],
)
def test_matched_act_passes(plan):
    cat = world()
    perform(cat, SIG, plan)
    _passes(cat)


def test_matched_restore_passes():
    cat = world()
    cat.commit(SIG, "WRITE", rows=[signal_row(1)])
    v = cat.latest_version(SIG) - 1
    perform(
        cat, SIG, [Planned("RESTORE", {"version": str(v)}, "")], run=lambda p: cat.restore(SIG, v)
    )
    _passes(cat)


@pytest.mark.parametrize(
    ("op", "params"),
    [
        ("UPDATE", {"predicate": "[]"}),
        ("DELETE", {"predicate": "[]"}),
        ("DROP CONSTRAINT", {"constraint": "action_valid"}),
        ("SET TBLPROPERTIES", {"properties": '{"delta.appendOnly":"true"}'}),
        ("UNSET TBLPROPERTIES", {"properties": '["delta.appendOnly"]'}),
    ],
)
def test_forbidden_or_act_only_outside_an_act(op, params):
    cat = world()
    cat.commit(SIG, op, params, user="someone@example.com")
    o = _fails(cat)
    assert str(cat.latest_version(SIG)) in o.detail


def test_unset_then_update_then_reset_within_the_hour():
    cat = world()
    good = cat.latest_version(SIG)
    props = cat.properties(SIG)
    cat.commit(
        SIG,
        "SET TBLPROPERTIES",
        {"properties": '{"delta.appendOnly":"false"}'},
        props={**props, "delta.appendOnly": "false"},
    )
    cat.commit(SIG, "UPDATE", {}, rows=[signal_row(1, "sell")])
    cat.commit(SIG, "SET TBLPROPERTIES", {"properties": '{"delta.appendOnly":"true"}'}, props=props)
    _fails(cat)
    assert cat.incident_rows[0].first_bad_version == good + 1


def test_changed_id_and_latest_below_cursor_as_recorded():
    text = (FIXTURES / "delta-history-drop-recreate.out").read_text()
    assert "id changed: True | latest < checked_version: True" in text
    cat = world()
    for i in range(3):
        cat.commit(SIG, "WRITE", rows=[signal_row(j) for j in range(i + 1)])
    _passes(cat)
    cat.drop(SIG)
    cat.execute(SPECS[SIG].create_sql())
    _fails(cat)
    assert cat.incident_rows[-1].kind == "replaced"


def test_constraint_with_other_text_fails_even_inside_an_act():
    cat = world()
    perform(
        cat,
        RECS,
        [
            Planned(
                "DROP CONSTRAINT",
                {"constraint": "never_flip"},
                f"ALTER TABLE {RECS} DROP CONSTRAINT never_flip",
            ),
            Planned(
                "ADD CONSTRAINT",
                {"name": "never_flip", "expr": "1=1"},
                f"ALTER TABLE {RECS} ADD CONSTRAINT never_flip CHECK (1=1)",
            ),
        ],
    )
    _fails(cat, RECS)
    assert "never_flip" in cat.incident_rows[-1].kind


def test_unknown_operation_rules():
    cat = world()
    cat.commit(SIG, "COMPUTE STATS", {"columns": "[]"}, user="po")
    o, _ = _guard(cat)
    assert o.kind == "warn"
    cat.commit(SIG, "REORG", {}, rows=[signal_row(9)], user="po")
    _fails(cat)
    cat2 = world()
    cat2.commit(SIG, "COMPUTE STATS", {"properties": '{"delta.appendOnly":"false"}'}, user="po")
    _fails(cat2)


@pytest.mark.parametrize(
    "change",
    [
        {"delta.deletedFileRetentionDuration": "interval 1 days"},
        {"delta.enableChangeDataFeed": "false"},
    ],
)
def test_state_check_fails_inside_a_matched_act(change):
    cat = world()
    body = ", ".join(f"'{k}' = '{v}'" for k, v in change.items())
    perform(
        cat,
        SIG,
        [
            Planned(
                "SET TBLPROPERTIES",
                {"properties": json.dumps(change, separators=(",", ":"))},
                f"ALTER TABLE {SIG} SET TBLPROPERTIES ({body})",
            )
        ],
    )
    _fails(cat)


def test_row_filter_fails():
    cat = world()
    cat.tables[SIG].filters = 1
    _fails(cat)


def test_not_found_is_replaced():
    cat = world()
    cat.drop(SIG)
    _fails(cat)
    assert cat.incident_rows[-1].kind == "replaced"


def test_other_read_error_is_unknown_and_holds_writers():
    cat = world()
    _guard(cat)
    assert check.may_write(cat.guard_rows(), AS_OF, "pre")
    later = utc(2026, 12, 2, 13, 30)
    cat.unreadable.add(SIG)
    o, _ = _guard(cat, as_of=later)
    assert o.kind == "unknown" and not cat.incident_rows
    assert cat.guard[SIG].pre_verified_as_of == AS_OF
    assert not check.may_write(cat.guard_rows(), later, "pre")


def test_merge_inside_an_act_still_matches():
    cat = world()
    plan = [
        Planned("DROP CONSTRAINT", {"constraint": "action_valid"}, ""),
        Planned(
            "ADD CONSTRAINT",
            {"name": "action_valid", "expr": "action IN ('buy', 'sell', 'hold')"},
            "",
        ),
    ]

    def run(p):
        if p.operation == "DROP CONSTRAINT":
            cat.execute(f"ALTER TABLE {SIG} DROP CONSTRAINT action_valid")
            cat.commit(SIG, "MERGE", {}, rows=[signal_row(1)], user="runner")
        else:
            cat.execute(
                f"ALTER TABLE {SIG} ADD CONSTRAINT action_valid CHECK (action IN ('buy', 'sell', 'hold'))"
            )

    act = perform(cat, SIG, plan, run=run)
    assert act.status == "complete"
    _passes(cat)


@pytest.mark.parametrize("variant", ["text", "user"])
def test_act_with_other_text_or_user_does_not_match(variant):
    cat = world()
    plan = [Planned("ADD CONSTRAINT", {"name": "extra", "expr": "confidence >= 0"}, "")]

    def run(p):
        expr = "confidence >= -1" if variant == "text" else "confidence >= 0"
        cat.commit(
            SIG,
            "ADD CONSTRAINT",
            {"name": "extra", "expr": expr},
            user="intruder" if variant == "user" else cat.user,
        )

    act = perform(cat, SIG, plan, run=run)
    assert act.status != "complete"
    _fails(cat)


def test_commit_between_history_read_and_cursor_write():
    cat = world()
    cat.commit(SIG, "WRITE", rows=[signal_row(1)])
    read_upto = cat.latest_version(SIG)
    real = cat.history

    def racing(table, after):
        out = real(table, after)
        if table == SIG and not racing.done:
            racing.done = True
            cat.commit(SIG, "UPDATE", {}, rows=[signal_row(1, "sell")], user="intruder")
        return out

    racing.done = False
    cat.history = racing
    o, _ = _guard(cat)
    assert o.kind == "pass" and cat.guard[SIG].checked_version == read_upto
    cat.history = real
    _fails(cat)


def test_a_missing_guard_state_is_unknown_not_a_traceback():
    """A missing ops table is the guard's unknown, and writers hold."""
    cat = world()
    cat.missing.add("ops.guard_state")
    out = check.run(cat, SPECS.values(), NOW, "pre", NOW, lambda m: None)
    assert [o.kind for o in out] == ["unknown"]
