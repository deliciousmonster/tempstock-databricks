"""The phases in order, and the driver: done, run, done again; Unknown or a refusal stops the run."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from weather_signal import config
from weather_signal.deploy import (
    agent,
    artifacts,
    genie,
    gitfolder,
    plan,
    preflight,
    schedules,
    seed,
    wheel,
)
from weather_signal.deploy.cli import Stop, Unknown
from weather_signal.deploy.context import Ctx
from weather_signal.ingest.files import Unreadable

PRINCIPALS = {"runner": "weather-signal-runner", "agent": "weather-signal-agent"}
ENTITLEMENTS = ("workspace-access", "databricks-sql-access")
PIPELINE_SOURCE = "src/weather_signal_etl/transformations/pipeline.py"
FQ = re.compile(r'name=fq\("(\w+)", "(\w+)"\)')
DAILY_LIMIT = "free daily limit"
TRAIN_FIT = "threshold_wf v2"


class State(Enum):
    DONE = "done"
    NOT_DONE = "not done"


@dataclass(frozen=True)
class Phase:
    name: str
    kind: Literal["checked", "reconcile"]
    done: Callable[[Ctx], State]
    run: Callable[[Ctx], None]


def _is(ok: bool) -> State:
    return State.DONE if ok else State.NOT_DONE


def _never(ctx: Ctx) -> None:
    raise Stop(1, "this phase has no run")


def preflight_done(ctx: Ctx) -> State:
    preflight.check(ctx)
    return State.DONE


def _principals(ctx: Ctx) -> dict[str, list[str]]:
    rows = ctx.cli("service-principals", "list") or []
    return {
        role: [str(r["applicationId"]) for r in rows if r.get("displayName") == name]
        for role, name in PRINCIPALS.items()
    }


def principals_done(ctx: Ctx) -> State:
    found = _principals(ctx)
    for role, ids in found.items():
        if len(ids) > 1:
            raise Stop(2, f"{len(ids)} principals named {PRINCIPALS[role]}: {', '.join(ids)}")
    ctx.principals = {role: ids[0] for role, ids in found.items() if ids}
    return _is(len(ctx.principals) == len(PRINCIPALS))


def principals_run(ctx: Ctx) -> None:
    for role, name in PRINCIPALS.items():
        if role in ctx.principals:
            continue
        body = {"displayName": name, "entitlements": [{"value": e} for e in ENTITLEMENTS]}
        ctx.cli("service-principals", "create", "--json", json.dumps(body))
        ctx.say(f"principals: created {name}")


def _todo(ctx: Ctx, *, say: bool = True) -> list[plan.Planned]:
    r = ctx.run([*ctx.bundle("plan"), "-o", "json"])
    if r.rc != 0:
        raise Unknown(f"bundle plan: exit {r.rc}: {r.tail(3)}")
    try:
        doc = json.loads(r.out)
    except json.JSONDecodeError as err:
        raise Unknown("bundle plan output is not JSON") from err
    todo = plan.changes(doc)
    for p in todo if say else ():
        ctx.say(f"bundle: {p.action} {p.key}")
    return todo


def _schedules_live(ctx: Ctx) -> bool:
    """dev, and prod with --live; prod without it keeps the state it has deployed."""
    if ctx.goes_live():
        return True
    if ctx.scope.target != "prod":
        return False
    if ctx.was_live is None:
        ctx.was_live = schedules.deployed(ctx) == "live" or schedules.held(ctx)
    return ctx.was_live


def bundle_done(ctx: Ctx) -> State:
    """A live set the plan leaves alone stays live; any other change deploys with schedules paused."""
    if _schedules_live(ctx):
        ctx.pause = "UNPAUSED"
        if not _todo(ctx, say=False):
            return State.DONE
    ctx.pause = "PAUSED"
    return _is(not _todo(ctx))


def bundle_run(ctx: Ctx) -> None:
    if ctx.pause == "PAUSED" and _schedules_live(ctx):
        schedules.hold(ctx)
        ctx.say("bundle: deploying paused; the live phase, or a rerun if a phase stops, unpauses")
    r = ctx.run(ctx.bundle("deploy"))
    if r.rc != 0:
        raise Stop(1, f"bundle deploy failed (exit {r.rc}):\n{r.tail()}")
    for line in artifacts.prune(ctx):
        ctx.say(line)


def secret_scope(ctx: Ctx) -> str:
    s = ctx.scope
    named = {"dev": "weather-signal-dev", "prod": config.SECRET_SCOPE}
    return named.get(s.target, f"weather-signal-{s.prefix}")


def secret_done(ctx: Ctx) -> State:
    keys = ctx.cli("secrets", "list-secrets", secret_scope(ctx)) or []
    return _is(any(k.get("key") == config.SECRET_KEY for k in keys))


def secret_run(ctx: Ctx) -> None:
    argv = ["databricks", "secrets", "put-secret", secret_scope(ctx), config.SECRET_KEY]
    value = ctx.key_file.read_text(encoding="utf-8").strip()
    r = ctx.run([*argv, "--profile", ctx.scope.profile], stdin=value)
    if r.rc != 0:
        raise Stop(1, f"secrets put-secret failed (exit {r.rc}); the key is not in the message")


def seed_done(ctx: Ctx) -> State:
    raw = ctx.seed_from is None or not _raw_todo(ctx, ctx.seed_from)
    return _is(raw and not _ledger_todo(ctx))


def _raw_todo(ctx: Ctx, root: Path) -> list[seed.Entry]:
    """Archive files the raw volume lacks; a landed copy of the same request is kept and said."""
    vol = ctx.scope.raw_volume

    def same(e: seed.Entry) -> bool:
        try:
            landed = ctx.store.read(f"{vol}/{e.path}")
        except Unreadable as err:
            raise Unknown(f"reading {vol}/{e.path} back") from err
        if not seed.same_request(e.path, (root / "raw" / e.path).read_bytes(), landed):
            return False
        if e.path not in ctx.seed_kept:
            ctx.seed_kept.add(e.path)
            ctx.say(f"seed: kept {vol}/{e.path}: landed copy of the same request, another fetch")
        return True

    return seed.missing(ctx.seed, ctx.list_volume(vol), vol, same=same)


def _ledger_todo(ctx: Ctx) -> list[str]:
    """Files of `--ledger-from` the ledger volume lacks; a claim already there is never replaced."""
    if ctx.ledger_from is None:
        return []
    have = ctx.list_volume(ctx.scope.names.ledger)
    return [x for x in seed.files_under(ctx.ledger_from) if x not in have]


def seed_run(ctx: Ctx) -> None:
    if ctx.ledger_from is not None:
        n = seed.restore(ctx.ledger_from, _ledger_todo(ctx), ctx.store, ctx.scope.names.ledger)
        ctx.say(f"seed: restored {n} ledger files from {ctx.ledger_from}, before any ingest claims")
    if ctx.seed_from is None:
        return
    todo = _raw_todo(ctx, ctx.seed_from)
    n = seed.upload(ctx.seed_from, todo, ctx.store, ctx.scope.raw_volume)
    ledger = (
        "the ledger volume starts empty (the archive holds raw files only)"
        if ctx.ledger_from is None
        else "the ledger restored above"
    )
    ctx.say(
        f"seed: uploaded {n} of {len(ctx.seed)} archive files; "
        f"{seed.skipped_line(ctx.seed_skipped)}; {ledger}"
    )


def pipeline_tables(repo: Path) -> set[tuple[str, str]]:
    """The (schema, table) pairs the pipeline publishes, read from its source, never a list here."""
    return set(FQ.findall((repo / PIPELINE_SOURCE).read_text(encoding="utf-8")))


def pipeline_done(ctx: Ctx) -> State:
    want = pipeline_tables(ctx.repo)
    have: set[tuple[str, str]] = set()
    for schema in sorted({s for s, _ in want}):
        rows = ctx.cli("tables", "list", ctx.scope.catalog, f"{ctx.scope.prefix}{schema}") or []
        have |= {(schema, str(t["name"])) for t in rows}
    return _is(want <= have)


def pipeline_run(ctx: Ctx) -> None:
    ctx.bundle_run("weather_signal")


def setup_run(ctx: Ctx) -> None:
    app = ctx.app()
    app_sp = str((app or {}).get("service_principal_client_id") or "")
    ctx.setup(ctx.scope, ctx.principals["runner"], ctx.principals["agent"], app_sp)


def app_done(ctx: Ctx) -> State:
    if ctx.skip_app:
        ctx.say("app: skipped (--skip-app); the project is not complete without it")
        return State.DONE
    app = ctx.app()
    if app is None:
        raise Unknown(f"App {ctx.scope.app_name} is missing after the bundle phase")
    state = ((app.get("active_deployment") or {}).get("status") or {}).get("state")
    if state != "SUCCEEDED":
        return State.NOT_DONE
    stamp = _app_stamp(ctx)
    deployed = stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else ""
    if deployed != wheel.digest(ctx.repo, wheel.APP):
        ctx.say("app: app/ differs from what this machine last deployed; redeploying")
        return State.NOT_DONE
    return State.DONE


def _app_stamp(ctx: Ctx) -> Path:
    """The digest of app/ at this machine's last good `bundle run ui_app`; none: redeploy once."""
    return ctx.state_dir / f"app-{ctx.scope.target}-{ctx.scope.prefix}.digest"


def app_run(ctx: Ctx) -> None:
    r = ctx.run(ctx.bundle("run", "ui_app"))
    if r.rc != 0 and DAILY_LIMIT in (r.out + r.err).lower():
        raise Stop(75, "Free Edition stopped Apps for today; rerun after 00:30 UTC")
    if r.rc != 0:
        raise Stop(1, f"bundle run ui_app failed (exit {r.rc}):\n{r.tail()}")
    stamp = _app_stamp(ctx)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(wheel.digest(ctx.repo, wheel.APP) + "\n", encoding="utf-8")


def train_done(ctx: Ctx) -> State:
    """A registered refit of this fit, the champion or one recorded as failing its gate."""
    tags = ctx.champion_tags(ctx.scope) or {}
    return _is(bool(tags.get("fit_as_of")) and tags.get("fit") == TRAIN_FIT)


def train_run(ctx: Ctx) -> None:
    ctx.bundle_run("train")


def refresh_done(ctx: Ctx) -> State:
    n = ctx.scope.names
    rows = ctx.sql(f"SELECT count(*) AS n FROM {n.table('signals', 'signal_backtest')}")
    snap = ctx.store.exists(f"{n.volume_path('signals', 'ui')}/snapshot.json")
    if snap == "unknown":
        raise Unknown("snapshot.json presence did not answer")
    return _is(bool(rows) and int(rows[0]["n"]) > 0 and snap == "present")


def refresh_run(ctx: Ctx) -> None:
    ctx.bundle_run("backtest")
    ctx.bundle_run("ui_snapshot")


def catchup_run(ctx: Ctx) -> None:
    """The ingest job once, from each source's newest landed slot to now; idempotent by key."""
    ctx.bundle_run("ingest", "--params", "catch_up=true")


def live_done(ctx: Ctx) -> State:
    """Every schedule UNPAUSED where the set goes live or prod already was; else they stay paused."""
    if not _schedules_live(ctx):
        ctx.say(f"live: schedules stay paused on {ctx.scope.target}")
        return State.DONE
    if not ctx.goes_live():
        ctx.say("live: prod's schedules were live before this run and stay live")
    ctx.pause = "UNPAUSED"
    if _todo(ctx) or not schedules.all_unpaused(ctx):
        return State.NOT_DONE
    schedules.release(ctx)
    return State.DONE


def live_run(ctx: Ctx) -> None:
    """Deploy UNPAUSED when the plan has changes; then unpause what it left paused by hand."""
    if _todo(ctx, say=False):
        bundle_run(ctx)
    for line in schedules.set_pause(ctx, "UNPAUSED"):
        ctx.say(f"live: {line}")


PHASES = (
    Phase("preflight", "checked", preflight_done, _never),
    Phase("principals", "checked", principals_done, principals_run),
    Phase("bundle", "checked", bundle_done, bundle_run),
    Phase("secret", "checked", secret_done, secret_run),
    Phase("seed", "checked", seed_done, seed_run),
    Phase("catchup", "reconcile", lambda ctx: State.NOT_DONE, catchup_run),
    Phase("pipeline", "checked", pipeline_done, pipeline_run),
    Phase("setup", "reconcile", lambda ctx: State.NOT_DONE, setup_run),
    Phase("genie", "checked", lambda ctx: _is(genie.done(ctx)), genie.ensure),
    Phase("app", "checked", app_done, app_run),
    Phase("train", "checked", train_done, train_run),
    Phase("refresh", "checked", refresh_done, refresh_run),
    Phase("agent", "checked", lambda ctx: _is(agent.done(ctx)), agent.run),
    Phase("live", "checked", live_done, live_run),
    Phase("gitfolder", "checked", lambda ctx: _is(gitfolder.done(ctx)), gitfolder.run),
)


def drive(ctx: Ctx, phases: tuple[Phase, ...] = PHASES) -> int:
    for p in phases:
        try:
            if p.done(ctx) is State.DONE:
                ctx.say(f"{p.name}: done")
                continue
            p.run(ctx)
            if p.kind == "checked" and p.done(ctx) is not State.DONE:
                raise Stop(1, f"{p.name}: still not done after its run")
            ctx.say(f"{p.name}: ran")
        except Unknown as err:
            ctx.say(f"{p.name}: stopped, a probe did not answer: {err}")
            return 1
        except Stop as err:
            ctx.say(f"{p.name}: stopped: {err}")
            return err.code
    return 0
