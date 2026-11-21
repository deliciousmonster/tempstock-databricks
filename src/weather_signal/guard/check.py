"""The guard: identity, current state and history of each job-owned table, before and after `score`."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from weather_signal.catalog.ddl import TableSpec, state_problems
from weather_signal.guard.catalog import Catalog, GuardRow, Incident, NotFound, Unreadable
from weather_signal.guard.rules import Act, Commit, judge, match_act, trusted_versions

log = logging.getLogger(__name__)

Phase = Literal["pre", "post"]
Kind = Literal["pass", "warn", "incident", "unknown"]
REMINDER_EVERY = timedelta(hours=24)
Notify = Callable[[str], None]


@dataclass
class Outcome:
    table: str
    kind: Kind
    detail: str = ""
    new_incident: bool = False


def _verified(row: GuardRow, phase: Phase, as_of: datetime) -> None:
    if phase == "pre":
        row.pre_verified_as_of = as_of
    else:
        row.post_verified_as_of = as_of


def _open(cat: Catalog, row: GuardRow, inc: Incident, now: datetime, notify: Notify) -> Outcome:
    known = {i.first_bad_version for i in cat.incidents(row.table_id)}
    if inc.first_bad_version not in known:
        cat.add_incident(inc)
    row.open_incident = True
    row.first_bad_version = inc.first_bad_version
    row.last_bad_version = inc.last_bad_version
    row.last_notified_at = now
    cat.put_guard_row(row)
    msg = (
        f"guard_incident {inc.kind} on {row.table_name} from version {inc.first_bad_version}: "
        f"{', '.join(f'{c.version} {c.operation} by {c.user}' for c in inc.commits) or 'state'}"
    )
    notify(msg)
    return Outcome(row.table_name, "incident", msg, new_incident=True)


def _replaced(row: GuardRow, now: datetime) -> Incident:
    v = row.checked_version + 1
    return Incident(row.table_name, row.table_id, v, v, "replaced", [], [], [], None, now)


def open_replaced(cat: Catalog, row: GuardRow, now: datetime, notify: Notify) -> Outcome:
    """The guarded name holds no table, or another one: a `replaced` incident."""
    return _open(cat, row, _replaced(row, now), now, notify)


def _still_open(cat: Catalog, row: GuardRow, now: datetime, notify: Notify) -> Outcome:
    if row.last_notified_at is None or now - row.last_notified_at >= REMINDER_EVERY:
        row.last_notified_at = now
        cat.put_guard_row(row)
        notify(
            f"guard_incident still open on {row.table_name} from version {row.first_bad_version}"
        )
    return Outcome(row.table_name, "incident", "open incident")


def guard_table(
    cat: Catalog,
    spec: TableSpec,
    row: GuardRow,
    as_of: datetime,
    phase: Phase,
    now: datetime,
    notify: Notify,
) -> Outcome:
    name = spec.name
    try:
        tid = cat.table_id(name)
        latest = cat.latest_version(name)
    except NotFound:
        tid, latest = None, -1
    if row.open_incident:
        if tid is not None:
            _match_pending(cat, name, tid, row.checked_version)
        return _still_open(cat, row, now, notify)
    if tid is None or tid != row.table_id or latest < row.checked_version:
        return open_replaced(cat, row, now, notify)
    problems = state_problems(spec, cat.properties(name), cat.filters_and_masks(name))
    acts, commits = _match_pending(cat, name, tid, row.checked_version)
    trusted = trusted_versions(acts) | {
        v for a in cat.acts(name) if a.status == "complete" for v in a.observed
    }
    judged = [c for c in commits if c.version > row.checked_version]
    hv = judge(judged, trusted, lambda v: cat.version_diff_empty(name, v), not problems)
    if hv.verdict == "incident" or problems:
        first = hv.first_bad_version or row.checked_version + 1
        last = max((c.version for c in hv.bad), default=latest)
        return _open(cat, row, _incident(cat, row, hv.bad, first, last, now, problems), now, notify)
    if hv.cursor is not None:
        row.checked_version = hv.cursor
    _verified(row, phase, as_of)
    cat.put_guard_row(row)
    return Outcome(name, hv.verdict, "; ".join(hv.warnings))


def _match_pending(
    cat: Catalog, name: str, tid: str, cursor: int
) -> tuple[list[Act], list[Commit]]:
    """Match every pending act against history; a crash mid-act is completed here."""
    acts = [a for a in cat.acts(name) if a.status == "pending" and a.table_id in (None, tid)]
    commits = cat.history(name, after=min([cursor, *(a.read_version for a in acts)]))
    for a in acts:
        before = a.status
        match_act(a, commits)
        if a.status != before or a.observed:
            cat.put_act(name, a)
    return acts, commits


def _incident(
    cat: Catalog,
    row: GuardRow,
    bad: list[Commit],
    first: int,
    last: int,
    now: datetime,
    problems: list[str],
) -> Incident:
    name = row.table_name
    removed, added = cat.diff_since(name, first - 1)
    changes = cat.table_changes(name, first, last)
    kind = "tamper" if bad else "state: " + "; ".join(problems)
    return Incident(name, row.table_id, first, last, kind, bad, removed, added, changes, now)


def run(
    cat: Catalog,
    specs: Iterable[TableSpec],
    as_of: datetime,
    phase: Phase,
    now: datetime,
    notify: Notify,
) -> list[Outcome]:
    """Guard every job-owned table; an unreadable answer is `unknown` and moves nothing."""
    try:
        rows = {r.table_name: r for r in cat.guard_rows()}
    except (Unreadable, NotFound) as err:
        notify(f"guard unknown: ops.guard_state unreadable ({type(err).__name__})")
        return [Outcome("ops.guard_state", "unknown", "unreadable")]
    out = []
    for spec in specs:
        row = rows.get(spec.name)
        if row is None:
            out.append(Outcome(spec.name, "unknown", "no guard_state row; run setup"))
            continue
        try:
            out.append(guard_table(cat, spec, row, as_of, phase, now, notify))
        except Unreadable as err:
            notify(f"guard unknown on {spec.name}: {err}")
            out.append(Outcome(spec.name, "unknown", str(err)))
    return out


def may_write(rows: Iterable[GuardRow], as_of: datetime, phase: Phase) -> bool:
    """A writer writes only when no guarded table is open and every one is verified for `as_of`."""
    rows = list(rows)
    if not rows:
        return False
    for r in rows:
        seen = r.pre_verified_as_of if phase == "pre" else r.post_verified_as_of
        if r.open_incident or seen != as_of:
            return False
    return True


def failure_reason(outcomes: Iterable[Outcome], signal_rows_ok: bool | None) -> str | None:
    """One reason per failed run; the guard's wins, and `no_signal_rows` is not evaluated after it."""
    if any(o.kind in ("incident", "unknown") for o in outcomes):
        return "guard_incident"
    if signal_rows_ok is False:
        return "no_signal_rows"
    return None
