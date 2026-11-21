"""The proof's archive: quiet dev's jobs, copy everything a destroy would lose, then prove that
nothing landed since. Every step before the destroy Stops with nothing destroyed.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from weather_signal.agent import launchd
from weather_signal.deploy import proof_checks as pc
from weather_signal.deploy import proof_verify, schedules
from weather_signal.deploy.cli import Stop, Unknown, not_found

if TYPE_CHECKING:
    from weather_signal.deploy.context import Ctx
    from weather_signal.deploy.proof import Proof

DRAIN_WAIT_S, DRAIN_EVERY_S = 1800.0, 30.0
AGENT_EXIT_WAIT_S, AGENT_EXIT_EVERY_S = 120.0, 5.0  # launchd's ExitTimeOut is 15 s
LAUNCHCTL_NO_SERVICE = 113
# The signals tables the score job, the backtest and the agent write; the pipeline's are rebuilt.
TABLES = ("signal", "recommendations", "agent_runs", "recommendations_eval", "signal_backtest")


class Reads(Protocol):
    """What reading a volume needs: the proof, or `archive export`'s own source."""

    @property
    def ctx(self) -> Ctx: ...
    @property
    def read(self) -> Callable[[str], bytes | None]: ...


@dataclass
class Archived:
    files: dict[str, dict[str, bytes]] = field(default_factory=dict)  # volume name -> path -> bytes
    rows: dict[str, int] = field(default_factory=dict)  # table -> rows exported


def pause(p: Proof) -> list[str]:
    """Pause every UNPAUSED dev schedule, so no ingest lands a file while the archive runs."""
    return schedules.set_pause(p.ctx, "PAUSED") or ["no dev schedule was unpaused"]


def session(p: Proof) -> str:
    """The agent's launchd domain, or Stop when this session cannot see it (ssh, a cron job):
    there `launchctl print` fails for a loaded agent too, and would read as "not loaded"."""
    domain = f"gui/{os.getuid()}"
    if p.ctx.run(["launchctl", "print", domain]).rc != 0:
        raise Stop(2, f"launchctl cannot see {domain} from this session (ssh?); run the proof from "
                      "a Terminal in the Mac's logged-in session")  # fmt: skip
    return domain


def _agent(p: Proof) -> tuple[bool, bool]:
    """(loaded, running) for the agent's LaunchAgent; `launchctl print` exits 113 for a service
    the domain does not hold, and any other failure is Unknown, never "not loaded"."""
    target = f"{session(p)}/{launchd.LABEL}"
    r = p.ctx.run(["launchctl", "print", target])
    if r.rc == LAUNCHCTL_NO_SERVICE:
        return False, False
    if r.rc != 0:
        raise Unknown(f"launchctl print {target}: exit {r.rc}")
    return True, "state = running" in r.out


def drain(p: Proof) -> list[str]:
    """Wait until no dev job run and no agent run is active; past the bound, Stop."""
    waited = 0.0
    while True:
        active = [
            f"{name} run {run.get('run_id')}"
            for name, rows in proof_verify.jobs_by_name(p).items()
            for job in rows
            for run in p.ctx.cli("jobs", "list-runs", "--job-id", str(job["job_id"]), "--active-only") or []
        ]  # fmt: skip
        active += ["the agent's run"] if _agent(p)[1] else []
        if not active:
            return [f"no active dev runs after {waited:.0f} s"]
        if waited >= DRAIN_WAIT_S:
            raise Stop(
                1, f"still active after {waited:.0f} s: {', '.join(active)}; nothing destroyed"
            )
        p.ctx.sleep(DRAIN_EVERY_S)
        waited += DRAIN_EVERY_S


def unload_agent(p: Proof) -> list[str]:
    """`launchctl bootout` the agent, so no :40 run starts again, then wait until no agent process
    is left: one that started after the drain ends (launchd stops it within its ExitTimeOut)
    before anything is exported. Its plist is kept; bootstrap's agent phase loads it again."""
    lines = ["agent: not loaded"]
    if _agent(p)[0]:
        r = p.ctx.run(["launchctl", "bootout", f"gui/{os.getuid()}/{launchd.LABEL}"])
        if r.rc != 0:
            raise Stop(
                1, f"launchctl bootout {launchd.LABEL} failed (exit {r.rc}); nothing destroyed"
            )
        lines = ["agent: unloaded (plist kept); bootstrap's agent phase loads it again"]
    waited = 0.0
    while _agent_process(p):
        if waited >= AGENT_EXIT_WAIT_S:
            raise Stop(
                1, f"an agent process is still running after {waited:.0f} s; nothing destroyed"
            )
        p.ctx.sleep(AGENT_EXIT_EVERY_S)
        waited += AGENT_EXIT_EVERY_S
    return [*lines, f"agent: no agent process after {waited:.0f} s"]


def agent_pattern(p: Proof) -> str:
    """A `pgrep -f` pattern for the installed agent only: the plist's ProgramArguments, anchored,
    so an agent run from another checkout (a test) is not waited for."""
    path = launchd.plist_path(p.ctx.home)
    args = [str(p.main / ".venv-connect" / "bin" / "python"), "-m", "weather_signal.agent", "run"]
    if path.is_file():
        args = [str(x) for x in plistlib.loads(path.read_bytes()).get("ProgramArguments", args)]
    return "^" + re.sub(r"([][.^$*+?(){}|\\])", r"\\\1", " ".join(args)) + "( |$)"


def _agent_process(p: Proof) -> bool:
    pattern = agent_pattern(p)
    r = p.ctx.run(["pgrep", "-f", "--", pattern])
    if r.rc not in (0, 1):
        raise Unknown(f"pgrep -f {pattern!r}: exit {r.rc}")
    return r.rc == 0


def _exists(p: Reads, volume: str) -> bool:
    name = ".".join(volume.removeprefix("/Volumes/").split("/")[:3])
    r = p.ctx.run(["databricks", "volumes", "read", name, "--profile", p.ctx.scope.profile])
    if not_found(r):
        return False
    if r.rc != 0:
        raise Unknown(f"volumes read {name}: exit {r.rc}: {r.tail(3)}")
    return True


def _read(p: Reads, volume: str, path: str, size: int) -> bytes:
    try:
        data = p.read(f"{volume}/{path}")
    except Exception as err:
        raise Stop(1, f"archive: {volume}/{path} unreadable ({type(err).__name__})") from err
    if data is None or len(data) != size:
        got = "absent" if data is None else f"{len(data)} bytes"
        raise Stop(1, f"archive: {volume}/{path} is {got}, listed {size}; nothing destroyed")
    return data


def lost(p: Proof, deployed: bool) -> dict[str, tuple[str, dict[str, bytes]]]:
    """Per volume, (its path, the files a destroy would lose with their bytes): raw files the data
    folder lacks or holds with other bytes, and every ledger file. A deployed volume must exist."""
    return volumes(p, {"raw": p.seed_from / "raw", "ledger": None}, must_exist=deployed)


def volumes(
    p: Reads, local: dict[str, Path | None], *, must_exist: bool
) -> dict[str, tuple[str, dict[str, bytes]]]:
    """Per volume named in `local` (raw, ledger), its path and the files `local` lacks or holds
    with other bytes, each read back at its listed size; None compares nothing, so every file."""
    paths = {"raw": p.ctx.scope.raw_volume, "ledger": p.ctx.scope.names.ledger}
    out: dict[str, tuple[str, dict[str, bytes]]] = {}
    for name, here in local.items():
        volume = paths[name]
        if not _exists(p, volume):
            if must_exist:
                raise Stop(1, f"archive: {volume} does not exist, yet the scope deploys it")
            out[name] = (volume, {})
            continue
        have = {k: _read(p, volume, k, n) for k, n in p.ctx.list_volume(volume).items()}
        out[name] = (volume, {k: have[k] for k in pc.volume_only(have, here)})
    return out


def copy(dest: Path, name: str, volume: str, files: dict[str, bytes]) -> str:
    """Write each file below `dest`, never over one; the line the operator reads."""
    for path, data in files.items():
        put(dest / path, data)
    return f"{name}: {len(files)} files archived from {volume}, bytes checked"


def put(dest: Path, data: bytes) -> None:
    """Write a new archive file; an existing one is never overwritten."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with dest.open("xb") as f:
            f.write(data)
    except FileExistsError as err:
        raise Stop(2, f"archive: {dest} exists; pass a new --out") from err


def _count(p: Proof, table: str) -> int | None:
    r = p.ctx.run(["databricks", "tables", "get", table, "--profile", p.ctx.scope.profile])
    if not_found(r):
        return None
    if r.rc != 0:
        raise Unknown(f"tables get {table}: exit {r.rc}: {r.tail(3)}")
    return int(p.ctx.sql(f"SELECT count(*) AS n FROM {table}")[0]["n"])


def archive(p: Proof, deployed: bool) -> tuple[Archived, list[str]]:
    """Copy each volume-only file and each signals table into `out/archive/`; else Stop."""
    got, lines = Archived(), []
    for name, (volume, files) in lost(p, deployed).items():
        lines.append(copy(p.out / "archive" / name, name, volume, files))
        got.files[name] = files
    for t in TABLES:
        table = p.ctx.scope.names.table("signals", t)
        if _count(p, table) is None:
            lines.append(f"{table}: absent, nothing to export")
            continue
        rows: list[dict[str, Any]] = p.ctx.sql(f"SELECT * FROM {table}")
        text = "".join(json.dumps(r, default=str, sort_keys=True) + "\n" for r in rows)
        put(p.out / "archive" / "tables" / f"{t}.jsonl", text.encode())
        got.rows[table] = len(rows)
        lines.append(f"{table}: {len(rows)} rows exported as JSON lines")
    return got, lines


def unchanged(p: Proof, got: Archived, deployed: bool) -> list[str]:
    """List and count again just before the destroy: anything new or changed since the archive
    Stops it, since that copy would hold none of it."""
    for name, (volume, files) in lost(p, deployed).items():
        new = sorted(k for k, data in files.items() if got.files.get(name, {}).get(k) != data)
        if new:
            raise Stop(1, f"{len(new)} files in {volume} landed after the archive (first {new[0]}); nothing destroyed")  # fmt: skip
    for table, n in got.rows.items():
        now = _count(p, table)
        if now != n:
            raise Stop(1, f"{table} holds {now} rows, {n} archived; nothing destroyed")  # fmt: skip
    return ["nothing landed or changed since the archive"]
