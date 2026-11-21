"""`python -m weather_signal.agent replay-refused`: rows moved to `runs-refused.jsonl`, tried again.

Run it by hand after a schema deploy that cures the refusal; rows the table still refuses stay.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from weather_signal.agent import runlog
from weather_signal.agent.route import error_text
from weather_signal.agent.store import Store

LOCK = "runs-refused.lock"  # beside the file, since the rewrite replaces the file's inode
QUARANTINE = "runs-refused.bad"  # lines replay could not read, kept verbatim for a person

log = logging.getLogger(__name__)


@contextmanager
def locked(cache: Path) -> Iterator[None]:
    """The hour's append and the replay's read and rewrite hold this; the inserts run outside it."""
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / LOCK).open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def append(cache: Path, row: dict[str, Any]) -> None:
    """The hour's move aside. A torn last line gets its newline first, so this row stands alone."""
    path = cache / runlog.REFUSED
    with locked(cache):
        if path.exists() and path.stat().st_size:
            with path.open("rb") as f:
                f.seek(-1, os.SEEK_END)
                torn = f.read(1) != b"\n"
            if torn:
                with path.open("a", encoding="utf-8") as f:
                    f.write("\n")
        runlog.append(path, row)


def _parse(data: bytes) -> tuple[list[dict[str, Any]], list[bytes]]:
    """Rows by first occurrence of each run_id, and the lines that are not a row with a run_id."""
    rows: dict[str, dict[str, Any]] = {}
    bad: list[bytes] = []
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:  # UnicodeDecodeError included: the line is kept as its bytes
            row = None
        if isinstance(row, dict) and isinstance(row.get("run_id"), str):
            rows.setdefault(row["run_id"], row)
        else:
            bad.append(line)
    return list(rows.values()), bad


def _quarantine(cache: Path, lines: list[bytes]) -> None:
    """Append verbatim and fsync; the rewrite that drops these lines runs only after this returns.
    A line already there is skipped, so a crash before the rewrite never quarantines it twice."""
    kept = set(_read(cache / QUARANTINE).split(b"\n"))
    lines = [line for line in dict.fromkeys(lines) if line not in kept]
    if not lines:
        return
    with (cache / QUARANTINE).open("ab") as f:
        f.write(b"".join(line + b"\n" for line in lines))
        f.flush()
        os.fsync(f.fileno())


def _read(path: Path) -> bytes:
    return path.read_bytes() if path.exists() else b""


def replay(cache: Path, store: Store) -> tuple[int, int]:
    """Rows now in the table (inserted or already there) and rows still refused, in that order.

    Lines appended while the rows insert are carried into the rewrite; unreadable lines go to
    QUARANTINE. Each run_id is tried and counted once.
    """
    path = cache / runlog.REFUSED
    with locked(cache):
        first = _read(path)
    found, bad = _parse(first)
    rows = [{k: v for k, v in r.items() if k != "refused"} for r in found]
    left: list[dict[str, Any]] = []
    if rows:
        present = store.run_ids_present([r["run_id"] for r in rows])
        for r in (r for r in rows if r["run_id"] not in present):
            try:
                store.insert_run(r)
            except Exception as err:  # noqa: BLE001 - kept in the file for the next replay
                left.append({**r, "refused": error_text(err)})
    if not first:
        return 0, 0
    with locked(cache):
        now = _read(path)
        if not now.startswith(first):
            raise RuntimeError(f"{path} was rewritten during the replay; left as it is")
        later, bad_later = _parse(now[len(first) :])
        ids = {r["run_id"] for r in left}
        tmp = path.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        tmp.touch()
        for r in [*left, *(r for r in later if r["run_id"] not in ids)]:
            runlog.append(tmp, r)
        _quarantine(cache, [*bad, *bad_later])
        os.replace(tmp, path)
    if bad or bad_later:
        log.warning("%d unreadable lines moved to %s", len(bad) + len(bad_later), QUARANTINE)
    return len(rows) - len(left), len(left)


def main() -> int:
    from weather_signal.agent.hour import connect_spark, profile
    from weather_signal.agent.options import CACHE
    from weather_signal.agent.store import SparkStore
    from weather_signal.config import Names

    done, left = replay(CACHE, SparkStore(connect_spark(profile()), Names.from_env()))
    sys.stdout.write(f"replayed {done}; still refused {left}\n")
    return 0 if left == 0 else 1
