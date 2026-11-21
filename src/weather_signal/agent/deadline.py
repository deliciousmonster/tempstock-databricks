"""The hard deadline (a `faulthandler` watchdog, never cancelled, with a SIGKILL backstop process 15 s
later) and the exit sequence that ends every run. When the worker leads its process group, as under
launchd, the backstop also SIGKILLs the group once the worker is gone, so no child outlives it.

A clean run ends with `os._exit`, so no thread left behind can hold the process past its hour.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import IO, NoReturn

from weather_signal.agent.budget import HARD_S

BACKSTOP_S = 15.0
# The dump was measured hanging mid-dump; launchd's exit cleanup only SIGTERMs the group (measured),
# so the watchdog ignores SIGTERM and SIGKILLs the group itself.
_WATCHDOG = """
import os, signal, sys, time
parent, limit = int(sys.argv[1]), time.monotonic() + float(sys.argv[2])
leader = os.getpgid(0) == parent
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while os.getppid() == parent:
    if time.monotonic() > limit:
        os.kill(parent, signal.SIGKILL)
        break
    time.sleep(0.5)
if leader:
    os.killpg(parent, signal.SIGKILL)
"""


class Deadline:
    """The open dump file; `arm` writes the main thread's id first so the dump can be read back."""

    def __init__(self, path: Path):
        self.path = path
        self.file: IO[str] | None = None
        self.watchdog: subprocess.Popen[bytes] | None = None

    def arm(self, seconds: float = HARD_S) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("w", encoding="utf-8")
        self.file.write(f"main 0x{threading.main_thread().ident or 0:x}\n")
        self.file.flush()
        faulthandler.dump_traceback_later(seconds, exit=True, file=self.file)
        self.watchdog = subprocess.Popen(
            [sys.executable, "-I", "-c", _WATCHDOG, str(os.getpid()), str(seconds + BACKSTOP_S)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def remove(self) -> None:
        if self.file is not None:
            self.file.close()
        self.path.unlink(missing_ok=True)


def finish(rc: int, deadline: Deadline | None) -> NoReturn:
    """Delete the deadline file, shut logging down, then `os._exit(rc)`: no atexit, no thread join.

    `runs.jsonl` is fsynced at every append, so nothing of it is left to flush here.
    """
    if deadline is not None:
        deadline.remove()
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        stream.flush()
    os._exit(rc)
