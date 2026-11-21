"""Old upload folders: each code change deploys under a new `artifacts/<tag>`, and nothing else
removes the last one. After a dev or scratch deploy, the tag folders under this bundle's own
artifacts path go, but for this deploy's and the newest other one, which a run started before the
deploy may still install from. Anything that is not plainly a tag folder is left alone; prod is
never pruned."""

from __future__ import annotations

import json
import re

from weather_signal.deploy.cli import Unknown, not_found
from weather_signal.deploy.context import Ctx
from weather_signal.deploy.scope import FIXED_PREFIX, SCRATCH_PREFIX

BUNDLE = "weather-signal"
# wheel.tag's digest, or the default a hand `bundle deploy` passes.
TAG = re.compile(r"[0-9a-f]{12}|manual")


def root(ctx: Ctx, me: str) -> str:
    """The deployed `artifact_path` without its tag, as databricks.yml roots each target."""
    s = ctx.scope
    folder = "dev" if s.target == "dev" and FIXED_PREFIX["dev"] == s.prefix else None
    if s.target == "scratch" and SCRATCH_PREFIX.fullmatch(s.prefix):
        folder = f"scratch-{s.prefix}"
    if folder is None or not me or "/" in me:
        raise Unknown(
            f"no artifacts path for target {s.target!r}, prefix {s.prefix!r}, user {me!r}"
        )
    return f"/Workspace/Users/{me}/.bundle/{BUNDLE}/{folder}/artifacts"


def stale(ctx: Ctx, base: str, current: str) -> list[str]:
    """The tag folders directly under `base` but for `current` and the newest other; none unless
    `current` is listed, and none when a folder's age does not read."""
    r = ctx.run(["databricks", "workspace", "list", base, "--profile", ctx.scope.profile,
                 "-o", "json"])  # fmt: skip
    if not_found(r):
        return []
    if r.rc != 0:
        raise Unknown(f"workspace list {base}: exit {r.rc}: {r.tail(1)}")
    try:
        rows = json.loads(r.out or "null")
    except json.JSONDecodeError as err:
        raise Unknown(f"workspace list {base}: output is not JSON") from err
    # The API may answer a /Workspace path without that prefix.
    inside = base.removeprefix("/Workspace") + "/"
    ages = {
        str(x.get("path")).removeprefix("/Workspace").removeprefix(inside): x.get("modified_at")
        for x in (rows if isinstance(rows, list) else [])
        if isinstance(x, dict) and x.get("object_type") == "DIRECTORY"
        and str(x.get("path")).removeprefix("/Workspace").startswith(inside)
    }  # fmt: skip
    if current not in ages:
        raise Unknown(f"{base} does not list the folder this deploy uploaded ({current})")
    other = {n: a for n, a in ages.items() if n != current and TAG.fullmatch(n)}
    if any(not isinstance(a, int) or isinstance(a, bool) for a in other.values()):
        raise Unknown(f"workspace list {base}: a tag folder has no modified_at")
    keep = max(other, key=lambda n: (other[n], n), default=None)
    return sorted(f"{base}/{n}" for n in other if n != keep)


def prune(ctx: Ctx) -> list[str]:
    """Delete the old tag folders after a deploy; a probe that does not answer deletes nothing."""
    current = ctx.wheel_tag
    if ctx.scope.target == "prod":
        return []
    try:
        if not TAG.fullmatch(current) or current == "manual":
            raise Unknown(f"deploy tag {current!r} is not a digest")
        me = str((ctx.cli("current-user", "me") or {}).get("userName") or "")
        base = root(ctx, me)
        old = stale(ctx, base, current)
    except Unknown as err:
        return [f"artifacts: old upload folders left: {err}"]
    lines = []
    for path in old:
        r = ctx.run(["databricks", "workspace", "delete", path, "--recursive",
                     "--profile", ctx.scope.profile])  # fmt: skip
        lines.append(f"artifacts: removed {path}" if r.rc == 0 else
                     f"artifacts: {path} not removed (exit {r.rc}): {r.tail(1)}")  # fmt: skip
    return lines
