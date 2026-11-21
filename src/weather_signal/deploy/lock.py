"""Bootstrap's one-run-per-scope lock, which the dev proof holds for its whole run and hands down.

The proof passes its open lock descriptor to the bootstrap it starts; `flock` on that inherited
description succeeds, where a fresh open of the same file by anyone else is refused.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ENV = "WEATHER_SIGNAL_BOOTSTRAP_LOCK_FD"


class Busy(Exception):
    """Another process holds the lock; this one sends nothing."""


def path(state_dir: Path, target: str, prefix: str) -> Path:
    """The lock file for `bootstrap-<target>-<prefix>`."""
    return state_dir / f"bootstrap-{target}-{prefix}.lock"


@contextmanager
def held(lock: Path) -> Iterator[int]:
    """This process's own exclusive lock; the descriptor, to hand to a child. Busy if taken."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as err:
            raise Busy(f"another bootstrap or proof holds {lock}") from err
        yield fd
    finally:
        os.close(fd)  # closing the last descriptor releases the lock


def _inherited(lock: Path) -> int | None:
    """The descriptor a parent proof passed for this same file, else None."""
    raw = os.environ.get(ENV, "")
    if not raw.isdigit():
        return None
    try:
        have, want = os.fstat(int(raw)), os.stat(lock)
    except OSError:
        return None
    return int(raw) if (have.st_dev, have.st_ino) == (want.st_dev, want.st_ino) else None


@contextmanager
def bootstrap_lock(lock: Path) -> Iterator[None]:
    """The lock a parent proof handed down, or this run's own; Busy when someone else holds it."""
    fd = _inherited(lock)
    if fd is None:
        with held(lock):
            yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as err:
        raise Busy(f"another bootstrap or proof holds {lock}") from err
    yield
