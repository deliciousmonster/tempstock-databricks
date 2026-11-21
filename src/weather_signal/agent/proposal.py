"""The only Python that touches a decision: a date's outcome turned into the MERGE's bound values.

No field here is an action; the SQL writes the signal's own action, or `hold` on a downgrade.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

MODEL_REASONS = ("stale_inputs", "out_of_range", "inconsistent_inputs")
CODE_REASONS = ("llm_unavailable", "llm_incomplete", "constraint_rejected")


@dataclass(frozen=True)
class Proposal:
    target_date: str
    downgrade: bool
    reason: str | None
    rationale: str


@dataclass(frozen=True)
class MergeParams:
    target_date: str
    downgrade: bool
    downgrade_reason: str | None
    rationale: str


def template(missing: str) -> str:
    if missing not in CODE_REASONS:
        raise ValueError(f"not a code reason: {missing}")
    return f"No explanation this hour ({missing}); held."


def params(d: str, p: Proposal | None, *, missing: str) -> MergeParams:
    """`d`'s values; a proposal written for any other date is a bug, never a row."""
    if p is None:
        return MergeParams(d, downgrade=True, downgrade_reason=missing, rationale=template(missing))
    if p.target_date != d:
        raise ValueError(f"proposal for {p.target_date} offered as {d}")
    return MergeParams(d, downgrade=p.downgrade, downgrade_reason=p.reason, rationale=p.rationale)


def fill(
    dates: Iterable[str], proposals: Mapping[str, Proposal], *, missing: str
) -> dict[str, MergeParams]:
    """Every date bound: its proposal if one was accepted, else a downgrade naming `missing`."""
    return {d: params(d, proposals.get(d), missing=missing) for d in dates}
