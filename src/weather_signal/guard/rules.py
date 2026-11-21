"""The guard's judgement of a table's history, pure: commits and acts in, verdict out (steps 3 and 4)."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

ALLOWED_ANYWHERE = frozenset(
    {"WRITE", "MERGE", "OPTIMIZE", "LIQUID TAGGING", "VACUUM START", "VACUUM END"}
)
ACT_ONLY = frozenset(
    {
        "CREATE TABLE",
        "ADD CONSTRAINT",
        "DROP CONSTRAINT",
        "SET TBLPROPERTIES",
        "UNSET TBLPROPERTIES",
        "RESTORE",
    }
)
FORBIDDEN = frozenset({"UPDATE", "DELETE"})


@dataclass(frozen=True)
class Commit:
    version: int
    operation: str
    parameters: dict[str, Any]
    user: str
    timestamp: datetime | None = None


@dataclass(frozen=True)
class PlannedCommit:
    operation: str
    parameters: dict[str, Any]


@dataclass
class Act:
    act_id: str
    table_id: str | None
    read_version: int
    user: str
    planned: list[PlannedCommit]
    status: str = "pending"
    observed: list[int] = field(default_factory=list)


def _value(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v.strip()
    return v


def _same_params(planned: dict[str, Any], seen: dict[str, Any]) -> bool:
    """Every key the act planned is in the commit with the same value."""
    for k, v in planned.items():
        if k not in seen:
            return False
        a, b = _value(v), _value(seen[k])
        if isinstance(a, str) and isinstance(b, str):
            a, b = "".join(a.split()).lower(), "".join(b.split()).lower()
        if a != b:
            return False
    return True


def matches(planned: PlannedCommit, c: Commit, user: str) -> bool:
    return (
        c.operation == planned.operation
        and c.user == user
        and _same_params(planned.parameters, c.parameters)
    )


def match_act(act: Act, commits: Sequence[Commit]) -> Act:
    """Consume, in order, the non-append commits after the act's read version that equal its plan."""
    i, observed, stray = 0, [], False
    for c in commits:
        if c.version <= act.read_version or c.operation in ALLOWED_ANYWHERE:
            continue
        if i < len(act.planned) and matches(act.planned[i], c, act.user):
            observed.append(c.version)
            i += 1
            continue
        stray = i < len(act.planned)
        break
    act.observed = observed
    act.status = "complete" if i == len(act.planned) else ("unmatched" if stray else "pending")
    return act


def trusted_versions(acts: Sequence[Act]) -> set[int]:
    """Versions a complete or still-pending act accounts for; an unmatched act vouches for none."""
    return {v for a in acts if a.status in ("complete", "pending") for v in a.observed}


Verdict = Literal["pass", "warn", "incident"]


@dataclass
class HistoryVerdict:
    verdict: Verdict
    bad: list[Commit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cursor: int | None = None

    @property
    def first_bad_version(self) -> int | None:
        return min(c.version for c in self.bad) if self.bad else None


def names_guarded_key(params: dict[str, Any]) -> bool:
    text = json.dumps(params, default=str).lower()
    return "delta." in text or "constraint" in text


def judge(
    commits: Sequence[Commit],
    act_versions: set[int],
    diff_empty: Callable[[int], bool | None],
    state_ok: bool,
) -> HistoryVerdict:
    """Step 3 over the commits after the cursor; step 4's cursor on a pass."""
    out = HistoryVerdict("pass")
    for c in commits:
        if c.version in act_versions or c.operation in ALLOWED_ANYWHERE:
            continue
        if c.operation in ACT_ONLY or c.operation in FORBIDDEN:
            out.bad.append(c)
            continue
        empty = diff_empty(c.version)
        if empty is True and not names_guarded_key(c.parameters) and state_ok:
            out.warnings.append(f"unknown operation {c.operation} at version {c.version}")
        else:
            out.bad.append(c)
    if out.bad:
        out.verdict = "incident"
        return out
    if commits:
        out.cursor = max(c.version for c in commits)
    if out.warnings:
        out.verdict = "warn"
    return out
