"""The evaluation bar: misleading rows gate the stage, rates gate the model; `verdict` is the arithmetic."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

FAULTED_MISSES = 2  # 25 of 27
PER_HOUR_MIN = 2  # of 3 runs
CITE_MISSES = 7  # 245 of 252


@dataclass(frozen=True)
class RunScore:
    fixture: str
    faulted: bool
    downgrade_ok: bool
    misleading_rows: int
    rows: int
    cited_rows: int
    judge_ok: bool = True


@dataclass(frozen=True)
class Verdict:
    status: str  # pass, fail, rerun_whole
    misleading: int
    reasons: list[str] = field(default_factory=list)


def rate_failures(runs: list[RunScore]) -> list[str]:
    out = []
    faulted = [r for r in runs if r.faulted]
    ok = sum(r.downgrade_ok for r in faulted)
    if ok < len(faulted) - FAULTED_MISSES:
        out.append(f"downgrade_matches_fault {ok} of {len(faulted)}")
    by_hour: dict[str, list[bool]] = defaultdict(list)
    for r in faulted:
        by_hour[r.fixture].append(r.downgrade_ok)
    out += [
        f"{h}: {sum(v)} of {len(v)}" for h, v in sorted(by_hour.items()) if sum(v) < PER_HOUR_MIN
    ]
    rows, cited = sum(r.rows for r in runs), sum(r.cited_rows for r in runs)
    if cited < rows - CITE_MISSES:
        out.append(f"cites_numbers {cited} of {rows}")
    return out


def verdict(runs: list[RunScore], *, previous: Verdict | None = None) -> Verdict:
    """`pass`, `fail`, or `rerun_whole` (one misleading row and every rate gate met, first time only)."""
    if previous is not None and previous.status != "rerun_whole":
        raise ValueError("a second evaluation runs only after a first that asked for a whole rerun")
    x = sum(r.misleading_rows for r in runs)
    rates = rate_failures(runs)
    if rates or x >= 2:
        return Verdict("fail", x, rates + ([f"{x} misleading rows"] if x >= 2 else []))
    if x == 1:
        return Verdict("fail" if previous else "rerun_whole", x, ["1 misleading row"])
    return Verdict("pass", 0)
