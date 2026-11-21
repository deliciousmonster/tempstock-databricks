"""The deployed pause state of the bundle's scheduled jobs, so a rerun keeps it."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from weather_signal.deploy.cli import Stop, Unknown
from weather_signal.deploy.context import Ctx

PAUSE_VAR = "pause_status: ${var.schedule_pause}"
JOB_NAME = re.compile(r"^\s+name: \$\{var\.schema_prefix\}([\w-]+)\s*$", re.MULTILINE)
SECTION = re.compile(r"^  \w+:\s*$")
Deployed = Literal["live", "paused", "absent"]


def scheduled_jobs(repo: Path) -> list[str]:
    """The unprefixed names of the jobs whose schedule takes the pause variable, read from resources/."""
    names = []
    for f in sorted((repo / "resources").glob("*.yml")):
        text = f.read_text(encoding="utf-8")
        heads = list(JOB_NAME.finditer(text))
        for i, m in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            if PAUSE_VAR in text[m.end() : end]:
                names.append(m.group(1))
    return names


def jobs(repo: Path) -> list[str]:
    """The unprefixed names of every job under a `resources: jobs:` key in resources/."""
    names = []
    for f in sorted((repo / "resources").glob("*.yml")):
        section = ""
        for line in f.read_text(encoding="utf-8").splitlines():
            if SECTION.match(line):
                section = line.strip().rstrip(":")
            m = JOB_NAME.match(line)
            if m and section == "jobs":
                names.append(m.group(1))
    return names


def _scheduled(ctx: Ctx) -> list[tuple[str, dict[str, Any]]]:
    """(prefixed name, `jobs list` row) for every deployed job whose schedule takes the variable."""
    out = []
    for name in scheduled_jobs(ctx.repo):
        full = f"{ctx.scope.prefix}{name}"
        rows = ctx.cli("jobs", "list", "--name", full) or []
        if not isinstance(rows, list):
            raise Unknown(f"jobs list --name {full}: not a list")
        out += [(full, r) for r in rows]
    return out


def deployed(ctx: Ctx) -> Deployed:
    """`live` when every deployed schedule is UNPAUSED; a mix is refused, since one deploy sets all."""
    found: dict[str, str] = {}
    for full, r in _scheduled(ctx):
        status = ((r.get("settings") or {}).get("schedule") or {}).get("pause_status")
        found[f"{full} ({r.get('job_id')})"] = str(status)
    states = set(found.values())
    if not states:
        return "absent"
    if states == {"UNPAUSED"}:
        return "live"
    if states == {"PAUSED"}:
        return "paused"
    lines = ", ".join(f"{k} {v}" for k, v in sorted(found.items()))
    raise Stop(
        2, f"the deployed schedules disagree ({lines}); pause or unpause them all, then rerun"
    )


def set_pause(ctx: Ctx, status: str) -> list[str]:
    """Set every deployed scheduled job to `status` with `jobs update`, the schedule otherwise kept;
    one line per job changed. A job paused by hand is drift a `bundle plan` may not show."""
    lines = []
    for full, r in _scheduled(ctx):
        sched = (r.get("settings") or {}).get("schedule")
        if not sched or sched.get("pause_status") == status:
            continue
        new = {"schedule": {**sched, "pause_status": status}}
        body = json.dumps({"job_id": r["job_id"], "new_settings": new})
        res = ctx.run(
            ["databricks", "jobs", "update", "--json", body, "--profile", ctx.scope.profile]
        )
        if res.rc != 0:
            raise Stop(1, f"jobs update {full} to {status} failed (exit {res.rc}): {res.tail(3)}")
        lines.append(f"{full} ({r['job_id']}) {status}")
    return lines


def all_unpaused(ctx: Ctx) -> bool:
    """Every deployed scheduled job UNPAUSED; a mix or any paused one is False, never a Stop."""
    rows = (r for _, r in _scheduled(ctx))
    return all(
        ((r.get("settings") or {}).get("schedule") or {}).get("pause_status") == "UNPAUSED"
        for r in rows
    )


def _held_path(ctx: Ctx) -> Path:
    return ctx.state_dir / f"bootstrap-{ctx.scope.target}-{ctx.scope.prefix}.live"


def hold(ctx: Ctx) -> None:
    """Record, before a live set deploys paused, that its schedules are meant to be live again."""
    path = _held_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{ctx.now().isoformat()} live set deployed paused\n", encoding="utf-8")


def held(ctx: Ctx) -> bool:
    """A paused deploy of a live set that no `live` phase has finished since."""
    return _held_path(ctx).is_file()


def release(ctx: Ctx) -> None:
    _held_path(ctx).unlink(missing_ok=True)
