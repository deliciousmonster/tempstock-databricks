"""Phase preflight: the owner's steps and the workspace's shape, read only; it never writes."""

from __future__ import annotations

import re
from dataclasses import replace

from weather_signal import config
from weather_signal.deploy import seed
from weather_signal.deploy.cli import Stop, Unknown, not_found
from weather_signal.deploy.context import Ctx
from weather_signal.deploy.scope import CATALOG, RAW_SCHEMA, SCRATCH_PREFIX, Scope

MIN_CLI = (1, 26, 0)
OLD_COMMIT = "9f88ad9"
LOGIN = "databricks auth login --host https://<your-workspace-host> --profile {p}"
NO_SEED = (
    "pass --seed-from <data folder> (e.g. ~/Developer/weather-signal-data); bootstrap fetches only "
    "the catch-up after it, so the history comes from that folder"
)
# One command: `uv run` creates .venv-connect and syncs it before running (the agent extra too).
CONNECT_RUN = (
    "UV_PROJECT_ENVIRONMENT=.venv-connect uv run --no-default-groups --group connect --extra agent "
    "weather-signal bootstrap"
)


def connect_line(ctx: Ctx, what: str) -> str:
    """The stop's one line: what is missing here, and the command that builds the venv and reruns."""
    return f"{what} is not importable here; run: {' '.join([CONNECT_RUN, *ctx.argv])}"


KEY = "Get a free key at https://www.alphavantage.co/support/#api-key and save it to {f}"


def check(ctx: Ctx) -> None:
    s = ctx.scope
    if not (ctx.repo / "databricks.yml").is_file():
        raise Stop(2, f"run bootstrap from the repository root; no databricks.yml in {ctx.repo}")
    _main_checkout(ctx)
    overrides = ctx.repo / ".databricks/bundle" / s.target / "variable-overrides.json"
    if overrides.exists():
        raise Stop(2, f"{overrides} outranks the target's variables; remove it")
    me = ctx.run(["databricks", "current-user", "me", "--profile", s.profile, "-o", "json"])
    if me.rc != 0:
        raise Stop(2, "not signed in to the workspace: " + LOGIN.format(p=s.profile))
    _cli_version(ctx)
    if not ctx.importable("databricks.connect"):
        line = connect_line(ctx, "databricks.connect (setup and refresh open a Spark session)")
        if ctx.needs_connect:
            raise Stop(2, line)
        ctx.say(f"preflight: note: {line}")
    if not ctx.key_file.is_file() or not ctx.key_file.read_text(encoding="utf-8").strip():
        raise Stop(2, KEY.format(f=ctx.key_file))
    if ctx.discover_catalog:
        s = ctx.scope = _default_catalog(ctx)
    cat = ctx.run(
        ["databricks", "catalogs", "get", s.catalog, "--profile", s.profile, "-o", "json"]
    )
    if not_found(cat):
        raise Stop(2, f"catalog {s.catalog} does not exist in this workspace")
    if cat.rc != 0:
        raise Unknown(f"catalogs get {s.catalog}: {cat.tail(3)}")
    ctx.warehouse_id = _warehouse(ctx)
    _schemas(ctx)
    if ctx.seed_from is not None:
        files, notes = seed.load(ctx.seed_from)
        ctx.seed, ctx.seed_skipped = seed.kept(ctx.seed_from, files)
        for line in [*notes, *seed.coverage(ctx.seed_from, ctx.seed)]:
            ctx.say(f"archive: {line}")
        gone = seed.absent_tickers(ctx.seed_from, ctx.seed, config.TICKERS)
        if gone:
            ctx.say(f"archive: no price history for {', '.join(gone)}; prices start when it lands")
    else:
        raise Stop(2, NO_SEED)


def _main_checkout(ctx: Ctx) -> None:
    """The LaunchAgent runs this checkout's venv, so a worktree that is later removed breaks it.
    `--ledger-from` restores a dev proof's archive, so it is refused anywhere else."""
    if ctx.ledger_from is not None and ctx.scope.target != "dev":
        raise Stop(2, f"--ledger-from restores a dev proof's archive; not on {ctx.scope.target}")
    main = ctx.linked_main(ctx.repo) if ctx.goes_live() else None
    if main is not None:
        raise Stop(
            2,
            f"{ctx.repo} is a linked worktree; the agent phase would pin the LaunchAgent to it. "
            f"Run bootstrap from the main checkout {main}",
        )


def _default_catalog(ctx: Ctx) -> Scope:
    """The metastore's default catalog for this workspace; `workspace` on Free Edition."""
    name = str((ctx.cli("metastores", "current") or {}).get("default_catalog_name") or "")
    if not CATALOG.fullmatch(name):
        raise Unknown(
            f"metastores current gave no plain default catalog ({name!r}); pass --catalog"
        )
    ctx.say(f"preflight: catalog {name} (the metastore's default)")
    return replace(ctx.scope, catalog=name)


def _cli_version(ctx: Ctx) -> None:
    r = ctx.run(["databricks", "version"])
    m = re.search(r"v?(\d+)\.(\d+)\.(\d+)", r.out)
    if r.rc != 0 or not m:
        raise Unknown("databricks version did not answer")
    have = tuple(int(x) for x in m.groups())
    if have < MIN_CLI:
        raise Stop(2, f"databricks CLI {'.'.join(map(str, have))} is older than 1.26.0; upgrade it")


def _warehouse(ctx: Ctx) -> str:
    rows = ctx.cli("warehouses", "list") or []
    if ctx.warehouse_name:
        rows = [w for w in rows if w.get("name") == ctx.warehouse_name]
    if len(rows) != 1:
        named = f" named {ctx.warehouse_name!r}" if ctx.warehouse_name else ""
        raise Stop(2, f"{len(rows)} SQL warehouses{named}; pass --warehouse-name to pick one")
    return str(rows[0]["id"])


def _schemas(ctx: Ctx) -> None:
    s = ctx.scope
    names = {str(x["name"]) for x in ctx.cli("schemas", "list", s.catalog) or []}
    if s.target == "scratch":
        others = sorted(
            {n.split("_", 1)[0] + "_" for n in names if SCRATCH_PREFIX.match(n)} - {s.prefix}
        )
        if others:
            raise Stop(
                2,
                f"another throwaway prefix is alive ({', '.join(others)}); remove it with "
                f"weather-signal bootstrap --profile {s.profile} --target scratch "
                f"--schema-prefix {others[0]} --destroy --yes",
            )
        return
    if f"{s.prefix}ops" in names and f"{s.prefix}{RAW_SCHEMA}" not in names:
        raise Stop(
            2,
            f"{s.prefix}ops exists without {s.prefix}{RAW_SCHEMA}: the old dev set is "
            "still there; the Lead destroys it before the first bootstrap",
        )
    _old_state(ctx)


def _old_state(ctx: Ctx) -> None:
    """The old dev bundle's state records the commit dev ran before the fresh land."""
    s = ctx.scope
    me = ctx.cli("current-user", "me")
    path = (
        f"/Workspace/Users/{me['userName']}/.bundle/weather-signal/{s.target}/state/metadata.json"
    )
    r = ctx.run(["databricks", "workspace", "export", path, "--profile", s.profile])
    if not_found(r):
        return
    if r.rc != 0:
        raise Unknown(f"workspace export {path}: {r.tail(3)}")
    if OLD_COMMIT in r.out:
        raise Stop(2, f"{path} records {OLD_COMMIT}, the old dev set; destroy it first")
