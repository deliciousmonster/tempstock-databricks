"""Throwaway launchd labels for the tests: bootstrapped from a temp dir, booted out and deleted in `finally`."""

import contextlib
import os
import plistlib
import subprocess
import uuid
from pathlib import Path

PREFIX = "dev.weather-signal.agent-test-"


def domain() -> str:
    return f"gui/{os.getuid()}"


@contextlib.contextmanager
def job(tmp: Path, doc: dict):
    label = doc.setdefault("Label", PREFIX + uuid.uuid4().hex[:8])
    assert label.startswith(PREFIX), "tests load only throwaway labels"
    path = tmp / f"{label}.plist"
    path.write_bytes(plistlib.dumps(doc))
    subprocess.run(["launchctl", "bootstrap", domain(), str(path)], check=True, capture_output=True)
    try:
        yield label
    finally:
        subprocess.run(
            ["launchctl", "bootout", f"{domain()}/{label}"], check=False, capture_output=True
        )
        path.unlink(missing_ok=True)


def loaded() -> list[str]:
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=False).stdout
    return [line.split()[-1] for line in out.splitlines() if PREFIX in line]
