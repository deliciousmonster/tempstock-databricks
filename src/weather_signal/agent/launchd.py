"""The LaunchAgent: `install` prints the plist and the `launchctl` lines, and runs them only with `--yes`."""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "dev.weather-signal.agent"
SERVICE = "workspace.default.weather_signal_agent"
GATEWAY_PROFILE = "weather-signal"
PROFILES = ("weather-signal-agent", "weather-signal-runner", "weather-signal")
BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def environment(profile: str, databricks_dir: str, prefix: str = "") -> dict[str, str]:
    """Exactly the seven names; no `DATABRICKS_*`, so nothing resolves a profile from defaults."""
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {PROFILES}")
    return {
        "CREW_LLM_BACKEND": "plan",
        "CREW_LLM_ROUTE": "databricks-gateway",
        "CREW_DATABRICKS_PROFILE": GATEWAY_PROFILE,
        "CREW_DATABRICKS_SERVICE": SERVICE,
        "WEATHER_SIGNAL_PROFILE": profile,
        "WEATHER_SIGNAL_SCHEMA_PREFIX": prefix,
        "PATH": f"{databricks_dir}:{BASE_PATH}",
    }


def plist(
    *,
    python: str,
    profile: str,
    databricks_dir: str,
    home: Path,
    prefix: str = "",
    minute: int = 40,
) -> bytes:
    cache = home / "Library" / "Caches" / "weather-signal" / "agent-cwd"
    logs = home / "Library" / "Logs" / "weather-signal" / "agent.log"
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [python, "-m", "weather_signal.agent", "run"],
            "StartCalendarInterval": {"Minute": minute},
            "EnvironmentVariables": environment(profile, databricks_dir, prefix),
            "WorkingDirectory": str(cache),
            "StandardOutPath": str(logs),
            "StandardErrorPath": str(logs),
            "ProcessType": "Background",
            "ExitTimeOut": 15,
        }
    )


def plist_path(home: Path, label: str = LABEL) -> Path:
    return home / "Library" / "LaunchAgents" / f"{label}.plist"


def commands(path: Path, label: str = LABEL) -> list[list[str]]:
    domain = f"gui/{os.getuid()}"
    return [
        ["launchctl", "bootout", f"{domain}/{label}"],
        ["launchctl", "bootstrap", domain, str(path)],
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m weather_signal.agent install")
    p.add_argument(
        "--profile", required=True, choices=PROFILES, help="the identity stage 5 recorded"
    )
    p.add_argument(
        "--schema-prefix", default="", help="the names the agent reads and writes; dev_ for dev"
    )
    p.add_argument("--yes", action="store_true", help="write the plist and bootstrap it")
    a = p.parse_args(argv)
    found = shutil.which("databricks")
    if not found:
        sys.stderr.write("refused: no `databricks` CLI on PATH; the gateway token fetch runs it\n")
        return 2
    home = Path.home()
    body = plist(
        python=sys.executable,
        profile=a.profile,
        databricks_dir=str(Path(found).parent),
        home=home,
        prefix=a.schema_prefix,
    )
    path = plist_path(home)
    sys.stdout.write(f"# {path}\n{body.decode()}\n")
    for c in commands(path):
        sys.stdout.write(" ".join(c) + "\n")
    if not a.yes:
        sys.stderr.write("# printed only; rerun with --yes to install\n")
        return 0
    if sys.platform != "darwin":
        sys.stderr.write(f"refused: launchd is macOS only; nothing installed at {path}\n")
        return 2
    _install(home, path, body)
    return 0


def _install(home: Path, path: Path, body: bytes) -> None:
    for d in (path.parent, home / "Library" / "Logs" / "weather-signal"):
        d.mkdir(parents=True, exist_ok=True)
    (home / "Library" / "Caches" / "weather-signal" / "agent-cwd").mkdir(
        parents=True, exist_ok=True
    )
    path.write_bytes(body)
    for c in commands(path):
        subprocess.run(c, check=False)
