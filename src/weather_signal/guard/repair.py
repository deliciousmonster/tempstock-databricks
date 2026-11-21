"""`weather-signal guard repair` and `guard ack`: the operator's acts on an open incident."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from weather_signal.catalog.ddl import Planned, TableSpec, state_problems
from weather_signal.guard.acts import latest_or_none, perform
from weather_signal.guard.catalog import (
    Catalog,
    GuardRow,
    Incident,
    NotFound,
    RestoreFailed,
    Row,
    Unreadable,
)


class RepairRefused(Exception):
    """The act was not started; nothing changed."""


def _row(cat: Catalog, table: str) -> GuardRow:
    for r in cat.guard_rows():
        if r.table_name == table:
            return r
    raise RepairRefused(f"no guard_state row for {table}; run setup")


def _incident(cat: Catalog, row: GuardRow) -> Incident:
    for inc in cat.incidents(row.table_id):
        if inc.first_bad_version == row.first_bad_version:
            return inc
    raise RepairRefused(f"no incident row for {row.table_name} at {row.first_bad_version}")


def _close(cat: Catalog, spec: TableSpec, row: GuardRow) -> str:
    problems = state_problems(spec, cat.properties(spec.name), cat.filters_and_masks(spec.name))
    if problems:
        return "incident stays open: " + "; ".join(problems)
    row.table_id = cat.table_id(spec.name)
    row.checked_version = cat.latest_version(spec.name)
    row.open_incident = False
    row.first_bad_version = row.last_bad_version = None
    row.updated_by = cat.current_user()
    cat.put_guard_row(row)
    return "repaired"


def _create_plan(spec: TableSpec) -> list[Planned]:
    plan = [Planned("CREATE TABLE", {}, spec.create_sql())]
    plan += [
        Planned(
            "ADD CONSTRAINT",
            {"name": n, "expr": e},
            f"ALTER TABLE {spec.name} ADD CONSTRAINT {n} CHECK ({e})",
        )
        for n, e in spec.constraints.items()
    ]
    return plan


def rebuilt_good_rows(current: list[Row], inc: Incident) -> list[Row]:
    """V_good's rows from the stored diff: current minus what was added plus what was removed."""

    def key(r: Row) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((k, repr(v)) for k, v in r.items()))

    added = Counter(key(r) for r in inc.added_since_good)
    out = []
    for r in current:
        k = key(r)
        if added[k]:
            added[k] -= 1
            continue
        out.append(r)
    return out + list(inc.removed_since_good)


def repair(
    cat: Catalog,
    spec: TableSpec,
    now: datetime,
    *,
    to_version: int | None = None,
    rebuild: bool = False,
) -> str:
    row = _row(cat, spec.name)
    if not row.open_incident or row.first_bad_version is None:
        raise RepairRefused(f"{spec.name} has no open incident")
    inc = _incident(cat, row)
    if inc.kind == "replaced":
        return _repair_replaced(cat, spec, row, inc)
    good = row.first_bad_version - 1
    if to_version is not None:
        if to_version > good:
            raise RepairRefused(f"--to-version {to_version} is after V_good {good}")
        good = to_version
    kept, dropped = cat.valid_new_rows(spec.name, spec.name, good, spec)
    inc.dropped = dropped
    cat.update_incident(inc)
    restore = Planned(
        "RESTORE", {"version": str(good)}, f"RESTORE TABLE {spec.name} TO VERSION AS OF {good}"
    )
    try:
        act = perform(cat, spec.name, [restore], run=lambda _p: cat.restore(spec.name, good))
    except RestoreFailed as err:
        if not rebuild:
            raise RepairRefused(f"RESTORE failed ({err}); rerun with --rebuild") from err
        return _rebuild(cat, spec, row, inc, kept)
    if act.status != "complete":
        return f"incident stays open: the RESTORE act is {act.status} against history"
    if kept:
        cat.insert(spec.name, kept)
    return _close(cat, spec, row)


def _rebuild(cat: Catalog, spec: TableSpec, row: GuardRow, inc: Incident, kept: list[Row]) -> str:
    good_rows = rebuilt_good_rows(cat.rows(spec.name), inc)
    cat.rename(spec.name, f"{spec.name}_tampered_{inc.first_bad_version}")
    perform(cat, spec.name, _create_plan(spec), read_version=-1)
    cat.insert(spec.name, good_rows + kept)
    return _close(cat, spec, row)


def _repair_replaced(cat: Catalog, spec: TableSpec, row: GuardRow, inc: Incident) -> str:
    """Put the guarded table back at its name: renamed back, undropped, never another table."""
    name = spec.name
    live = None if latest_or_none(cat, name) is None else cat.table_id(name)
    if live == row.table_id:
        return _original_back(cat, row)
    moved = f"{name}_replaced_{inc.first_bad_version}"
    if live is not None:
        cat.rename(name, moved)
    try:
        cat.undrop(name)
    except (Unreadable, NotFound) as err:
        _put_back(cat, name, moved, live)
        raise RepairRefused(_no_undrop(name, row, err)) from err
    got = cat.table_id(name)
    if got != row.table_id:
        cat.rename(name, f"{name}_undropped_{got[:8]}")
        _put_back(cat, name, moved, live)
        raise RepairRefused(
            f"UNDROP restored {got}, not the guarded table {row.table_id}; set aside as "
            f"{name}_undropped_{got[:8]}. {_rename_hint(name, row)}"
        )
    if live is not None:
        kept, dropped = cat.valid_new_rows(name, moved, None, spec)
        inc.dropped = dropped
        cat.update_incident(inc)
        if kept:
            cat.insert(name, kept)
    return _close(cat, spec, row)


def _put_back(cat: Catalog, name: str, moved: str, live: str | None) -> None:
    if live is not None:
        cat.rename(moved, name)


def _rename_hint(name: str, row: GuardRow) -> str:
    return (
        f"If table {row.table_id} was renamed away, ALTER TABLE <its name> RENAME TO {name}, "
        "then rerun guard repair."
    )


def _no_undrop(name: str, row: GuardRow, err: Exception) -> str:
    return (
        f"UNDROP {name} failed ({type(err).__name__}); nothing changed. {_rename_hint(name, row)}"
    )


def _original_back(cat: Catalog, row: GuardRow) -> str:
    """The original is at its name again; the cursor stays so the next check judges its history."""
    row.open_incident = False
    row.first_bad_version = row.last_bad_version = None
    row.updated_by = cat.current_user()
    cat.put_guard_row(row)
    return "repaired"


def ack(cat: Catalog, spec: TableSpec, through: int) -> str:
    row = _row(cat, spec.name)
    latest = cat.latest_version(spec.name)
    if through > latest:
        raise RepairRefused(f"version {through} is past the latest, {latest}")
    if row.last_bad_version is not None and through < row.last_bad_version:
        raise RepairRefused(
            f"version {through} is before the last bad version {row.last_bad_version}"
        )
    problems = state_problems(spec, cat.properties(spec.name), cat.filters_and_masks(spec.name))
    if any("constraint" in p for p in problems):
        raise RepairRefused("a constraint is missing or changed; run guard repair")
    row.checked_version = through
    row.open_incident = False
    row.first_bad_version = row.last_bad_version = None
    row.updated_by = cat.current_user()
    cat.put_guard_row(row)
    return "acknowledged"


def adopt_id(cat: Catalog, spec: TableSpec) -> str:
    """Take a deliberately recreated table's id and version as the new baseline."""
    problems = state_problems(spec, cat.properties(spec.name), cat.filters_and_masks(spec.name))
    if problems:
        raise RepairRefused("state check fails: " + "; ".join(problems))
    row = _row(cat, spec.name)
    perform(cat, spec.name, [])
    return _close(cat, spec, row)
