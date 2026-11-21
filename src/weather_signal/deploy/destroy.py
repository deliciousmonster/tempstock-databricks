"""`--destroy`: a throwaway `tN_` scope removed by its own bundle, then checked for residue."""

from __future__ import annotations

from weather_signal.deploy import genie
from weather_signal.deploy.cli import Stop
from weather_signal.deploy.context import Ctx
from weather_signal.deploy.scope import SCRATCH_PREFIX


def refuse_unless_scratch(target: str, prefix: str, yes: bool) -> None:
    """Checked before any call; dev, accept and prod are never destroyed by bootstrap."""
    if target != "scratch" or not SCRATCH_PREFIX.fullmatch(prefix or ""):
        raise Stop(
            2, f"--destroy takes only --target scratch with a tN_ prefix (got {target}, {prefix!r})"
        )
    if not yes:
        raise Stop(2, "--destroy needs --yes")


def residue(ctx: Ctx, tidy: bool = True) -> list[str]:
    s = ctx.scope
    left = [
        f"schema {x['name']}"
        for x in ctx.cli("schemas", "list", s.catalog) or []
        if str(x["name"]).startswith(s.prefix)
    ]
    left += [
        f"app {x['name']}"
        for x in ctx.cli("apps", "list") or []
        if str(x["name"]).startswith(s.app_prefix)
    ]
    left += [
        f"job {x['settings']['name']}"
        for x in ctx.cli("jobs", "list") or []
        if str(x.get("settings", {}).get("name", "")).startswith(s.prefix)
    ]
    left += [
        f"pipeline {x['name']}"
        for x in ctx.cli("pipelines", "list-pipelines") or []
        if str(x.get("name", "")).startswith(s.prefix)
    ]
    left += [f"genie space {genie.title(ctx)} {i}" for i in genie.found(ctx, tidy)]
    return left


def destroy(ctx: Ctx) -> int:
    refuse_unless_scratch(ctx.scope.target, ctx.scope.prefix, yes=True)
    r = ctx.run([*ctx.bundle("destroy"), "--auto-approve"])
    if r.rc != 0:
        raise Stop(1, f"bundle destroy failed (exit {r.rc}):\n{r.tail()}")
    # The space is bootstrap's, not the bundle's; its title carries the tN_ prefix.
    genie.trash(ctx)
    left = residue(ctx)
    for x in left:
        ctx.say(f"destroy: still present: {x}")
    ctx.say("destroy: nothing left" if not left else "destroy: residue above; nothing else deleted")
    return 1 if left else 0
