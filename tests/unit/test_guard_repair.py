"""`guard repair` and `guard ack`: restore to `V_good`, keep valid appends, rebuild, replaced."""

from collections import Counter

import pytest

from tests.conftest import utc
from tests.unit.fake_catalog import _key
from tests.unit.guard_world import RECS, SPECS, rec_row, world
from weather_signal.guard import check, repair
from weather_signal.guard.rules import Act, PlannedCommit

NOW = utc(2026, 12, 2, 13)
SPEC = SPECS[RECS]


def _guard(cat):
    return check.run(cat, SPECS.values(), NOW, "pre", NOW, lambda m: None)


def _tampered():
    cat = world()
    cat.insert(RECS, [rec_row(1), rec_row(2, "hold")])
    _guard(cat)
    good = cat.latest_version(RECS)
    good_rows = cat.rows(RECS)
    props = cat.properties(RECS)
    cat.commit(
        RECS,
        "SET TBLPROPERTIES",
        {"properties": '{"delta.appendOnly":"false"}'},
        props={**props, "delta.appendOnly": "false"},
        user="intruder",
    )
    cat.execute(f"ALTER TABLE {RECS} DROP CONSTRAINT never_flip")
    cat.commit(RECS, "UPDATE", {}, rows=[rec_row(1, "sell"), rec_row(2, "hold")], user="intruder")
    cat.insert(RECS, [rec_row(3, "sell", "buy")])  # violates never_flip
    cat.insert(RECS, [rec_row(4, "hold", "sell")])  # valid, key new
    _guard(cat)
    return cat, good, good_rows


def _same(a, b):
    return Counter(map(_key, a)) == Counter(map(_key, b))


def test_restore_to_v_good_and_keep_valid_new_rows():
    cat, good, good_rows = _tampered()
    row = cat.guard[RECS]
    assert row.first_bad_version == good + 1
    assert repair.repair(cat, SPEC, NOW) == "repaired"
    assert _same(cat.rows(RECS), good_rows + [rec_row(4, "hold", "sell")])
    inc = cat.incident_rows[0]
    assert _same(inc.dropped, [rec_row(3, "sell", "buy"), rec_row(1, "sell")])
    assert not cat.guard[RECS].open_incident
    assert check.run(cat, SPECS.values(), NOW, "pre", NOW, lambda m: None)[1].kind == "pass"


def test_act_row_precedes_restore():
    cat, good, _ = _tampered()
    repair.repair(cat, SPEC, NOW)
    act_i = max(i for i, s in enumerate(cat.log) if s == f"ACT pending {RECS}")
    restore_i = next(i for i, s in enumerate(cat.log) if s.startswith(f"RESTORE {RECS}"))
    assert act_i < restore_i


def test_extra_commit_inside_the_act_leaves_it_unmatched():
    cat, good, _ = _tampered()
    real = cat.restore

    def restore(table, v):
        cat.commit(RECS, "DELETE", {}, rows=[], user="intruder")
        real(table, v)

    cat.restore = restore
    assert repair.repair(cat, SPEC, NOW).startswith("incident stays open")
    act = [a for a in cat.acts(RECS) if any(p.operation == "RESTORE" for p in a.planned)][0]
    assert act.status == "unmatched"
    assert check.run(cat, SPECS.values(), NOW, "pre", NOW, lambda m: None)[1].kind == "incident"


def test_crash_after_restore_is_completed_by_next_check():
    cat, good, _ = _tampered()
    act = Act(
        "crash",
        cat.table_id(RECS),
        cat.latest_version(RECS),
        cat.user,
        [PlannedCommit("RESTORE", {"version": str(good)})],
    )
    cat.put_act(RECS, act)
    cat.restore(RECS, good)
    _guard(cat)
    assert [a.status for a in cat.acts(RECS) if a.act_id == "crash"] == ["complete"]


def test_rebuild_when_restore_fails():
    cat, good, good_rows = _tampered()
    old_id = cat.table_id(RECS)
    cat.restore_fails = True
    with pytest.raises(repair.RepairRefused):
        repair.repair(cat, SPEC, NOW)
    assert repair.repair(cat, SPEC, NOW, rebuild=True) == "repaired"
    assert _same(cat.rows(RECS), good_rows + [rec_row(4, "hold", "sell")])
    assert cat.table_id(RECS) != old_id and cat.guard[RECS].table_id == cat.table_id(RECS)
    assert f"{RECS}_tampered_{good + 1}" in cat.tables


def test_replaced_repair_moves_valid_rows_to_the_original():
    cat = world()
    cat.insert(RECS, [rec_row(1)])
    _guard(cat)
    original = cat.table_id(RECS)
    cat.drop(RECS)
    cat.create(RECS, {})
    cat.insert(RECS, [rec_row(5, "hold", "buy"), rec_row(6, "sell", "buy")])
    _guard(cat)
    assert repair.repair(cat, SPEC, NOW) == "repaired"
    assert cat.table_id(RECS) == original
    assert _same(cat.rows(RECS), [rec_row(1), rec_row(5, "hold", "buy")])


def test_adopt_id_refuses_while_state_fails():
    cat = world()
    cat.drop(RECS)
    cat.create(RECS, {})
    _guard(cat)
    with pytest.raises(repair.RepairRefused):
        repair.adopt_id(cat, SPEC)


def _replaced_by(tamper):
    cat = world()
    cat.insert(RECS, [rec_row(1)])
    _guard(cat)
    original = cat.table_id(RECS)
    tamper(cat)
    _guard(cat)
    assert cat.guard[RECS].open_incident
    return cat, original


def test_rename_away_refuses_with_the_rename_back_and_changes_nothing():
    """No traceback; the operator is told to rename the original back."""
    cat, original = _replaced_by(lambda c: c.rename(RECS, f"{RECS}_moved"))
    with pytest.raises(repair.RepairRefused, match="RENAME TO"):
        repair.repair(cat, SPEC, NOW)
    assert cat.table_id(f"{RECS}_moved") == original and RECS not in cat.tables
    assert cat.guard[RECS].open_incident


def test_the_original_renamed_back_closes_without_moving_the_cursor():
    cat, original = _replaced_by(lambda c: c.rename(RECS, f"{RECS}_moved"))
    cursor = cat.guard[RECS].checked_version
    cat.rename(f"{RECS}_moved", RECS)
    assert repair.repair(cat, SPEC, NOW) == "repaired"
    row = cat.guard[RECS]
    assert (row.table_id, row.open_incident, row.checked_version) == (original, False, cursor)


def test_a_drop_alone_is_undropped():
    cat, original = _replaced_by(lambda c: c.drop(RECS))
    assert repair.repair(cat, SPEC, NOW) == "repaired"
    assert cat.table_id(RECS) == original and _same(cat.rows(RECS), [rec_row(1)])


def _swap(cat):
    cat.rename(RECS, f"{RECS}_orig")
    cat.create(RECS, {})


def test_failed_undrop_puts_the_replacement_back():
    """The live name is never left empty."""
    cat, original = _replaced_by(_swap)
    replacement = cat.table_id(RECS)
    with pytest.raises(repair.RepairRefused, match="UNDROP"):
        repair.repair(cat, SPEC, NOW)
    assert cat.table_id(RECS) == replacement and cat.guard[RECS].open_incident


def test_an_undropped_stranger_is_not_adopted():
    """UNDROP restores the newest drop by name, which may be another table."""
    cat, original = _replaced_by(_swap)
    replacement = cat.table_id(RECS)
    cat.create("stale", {})
    stranger = cat.table_id("stale")
    cat.drop("stale")
    cat.dropped[RECS] = cat.dropped.pop("stale")
    with pytest.raises(repair.RepairRefused, match="not the guarded table"):
        repair.repair(cat, SPEC, NOW)
    assert cat.table_id(RECS) == replacement
    assert stranger not in {cat.guard[RECS].table_id, cat.table_id(RECS)}
    assert cat.guard[RECS].open_incident and cat.guard[RECS].table_id == original
