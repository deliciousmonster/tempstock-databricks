"""`runs.jsonl`: a `started` line before anything can hang, the full row at the end, and the next run's
reconciliation of every run id the table lacks.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from weather_signal.catalog.ddl import AGENT_OUTCOMES

OUTCOMES = AGENT_OUTCOMES
HOURLY, EVAL = "runs.jsonl", "runs-eval.jsonl"
REFUSED = "runs-refused.jsonl"  # rows the table turned down for a day; kept for a person to read
NEVER = frozenset({"email", "organization"})
_THREAD = re.compile(r"^(?:Current thread|Thread) (0x[0-9a-fA-F]+)")
_FRAME = re.compile(r'^\s+File "(.+)", line (\d+) in (.+)$')


def append(path: Path, row: dict[str, Any]) -> None:
    """One JSON line, flushed and fsynced before returning."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({k: v for k, v in row.items() if k not in NEVER}, default=str)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def deadline_path(cache: Path, run_id: str) -> Path:
    return cache / f"deadline-{run_id}.txt"


def innermost_main_frame(text: str) -> str | None:
    """The first frame (most recent call) of the main thread named on the file's `main` line."""
    main = re.search(r"^main (0x[0-9a-fA-F]+)$", text, re.M)
    want = int(main.group(1), 16) if main else None
    current: int | None = None
    for line in text.splitlines():
        t = _THREAD.match(line)
        if t:
            current = int(t.group(1), 16)
            continue
        f = _FRAME.match(line)
        if f and current is not None and current == want:
            return f"{Path(f.group(1)).name}:{f.group(2)} in {f.group(3)}"
    return None


@dataclass(frozen=True)
class Reconciled:
    rows: list[dict[str, Any]]
    deadline_files: list[Path]


def reconcile(log: Path, cache: Path, has_row: Callable[[Iterable[str]], set[str]]) -> Reconciled:
    """Rows for run ids in `log` that the table lacks: the first full line, else from the `started` line."""
    started: dict[str, dict[str, Any]] = {}
    full: dict[str, dict[str, Any]] = {}
    for line in read(log):
        rid = line.get("run_id")
        if not isinstance(rid, str):
            continue
        if line.get("line") == "started":
            started.setdefault(rid, line)
        else:
            full.setdefault(rid, line)
    ids = set(started) | set(full)
    done = has_row(sorted(ids)) if ids else set()
    rows, files = [], []
    for rid in sorted(ids - done):
        path = deadline_path(cache, rid)
        if path.exists():
            files.append(path)
        if rid in full:
            rows.append(_row(full[rid]))
            continue
        rows.append(_from_started(started[rid], path))
    return Reconciled(rows, files)


def _row(line: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in line.items() if k != "line"}


def _from_started(line: dict[str, Any], deadline: Path) -> dict[str, Any]:
    text = deadline.read_text(encoding="utf-8", errors="replace") if deadline.exists() else ""
    killed = any(_THREAD.match(line) for line in text.splitlines())
    row = _row(line)
    row.update(
        outcome="deadline_killed" if killed else "crashed",
        route="none",
        error=innermost_main_frame(text) if killed else None,
    )
    return row
