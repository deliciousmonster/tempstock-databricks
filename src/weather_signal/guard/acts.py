"""A guard act: the planned commits written ahead to `ops.guard_acts`, then run, then matched."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence

from weather_signal.catalog.ddl import Planned
from weather_signal.guard.catalog import Catalog, NotFound
from weather_signal.guard.rules import Act, PlannedCommit, match_act


def latest_or_none(cat: Catalog, table: str) -> int | None:
    try:
        return cat.latest_version(table)
    except NotFound:
        return None


def perform(
    cat: Catalog,
    table: str,
    planned: Sequence[Planned],
    run: Callable[[Planned], None] | None = None,
    read_version: int | None = None,
) -> Act:
    """Write the act row before the first commit, run each step, then match it against history."""
    if read_version is None:
        latest = latest_or_none(cat, table)
        read_version = -1 if latest is None else latest
    tid = None if read_version < 0 else cat.table_id(table)
    act = Act(
        act_id=uuid.uuid4().hex,
        table_id=tid,
        read_version=read_version,
        user=cat.current_user(),
        planned=[PlannedCommit(p.operation, p.parameters) for p in planned],
    )
    cat.put_act(table, act)
    for p in planned:
        (run or (lambda q: cat.execute(q.sql)))(p)
    return complete(cat, table, act)


def complete(cat: Catalog, table: str, act: Act) -> Act:
    if act.table_id is None:
        act.table_id = cat.table_id(table)
    match_act(act, cat.history(table, after=act.read_version))
    cat.put_act(table, act)
    return act
