"""`weather-signal bootstrap`: the parser, the real dependencies, and the lock around one run."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from weather_signal.deploy import baseline, destroy, lock, phases, preflight, proof_record, scope
from weather_signal.deploy.cli import Stop, Unknown, subprocess_runner
from weather_signal.deploy.context import Ctx
from weather_signal.deploy.lock import Busy
from weather_signal.deploy.seed import list_volume_sdk

STATE = Path.home() / ".local/state/weather-signal"
KEY_FILE = Path.home() / ".config/alphavantage/API_KEY"


def add_parser(sub: Any) -> None:
    b = sub.add_parser("bootstrap", help="recreate the project from the repo and the raw archive")
    b.add_argument("--profile", dest="b_profile", required=True)
    b.add_argument("--target", required=True, help="dev, scratch or prod")
    b.add_argument("--schema-prefix", dest="b_prefix", help="tN_ for --target scratch")
    b.add_argument("--catalog", dest="b_catalog", help="default: the metastore's default catalog")
    b.add_argument("--warehouse-name", help="the SQL warehouse, when there is more than one")
    b.add_argument("--seed-from", type=Path, help="an archive folder: manifest.json and raw/")
    b.add_argument("--ledger-from", type=Path, help="ledger files to restore before ingest runs")
    b.add_argument("--only", choices=["preflight"], help="run preflight alone; writes nothing")
    b.add_argument("--skip-app", action="store_true", help="leave the App undeployed")
    b.add_argument("--write-baseline", type=Path, help="write the scope's baseline to this file")
    b.add_argument("--cutoff", type=date.fromisoformat, help="baseline rows dated on or before")
    b.add_argument("--compare", type=Path, help="compare the scope with this baseline")
    b.add_argument("--destroy", action="store_true", help="remove a scratch tN_ scope")
    b.add_argument("--yes", action="store_true", help="confirm --destroy")
    b.add_argument("--live", action="store_true", help="prod: unpause schedules, install the agent")
    b.set_defaults(fn=main)


def main(a: argparse.Namespace) -> int:
    try:
        if a.destroy:
            destroy.refuse_unless_scratch(a.target, a.b_prefix, a.yes)
        s = scope.build(a.b_profile, a.target, a.b_prefix, a.b_catalog or "workspace")
    except (scope.ScopeError, Stop) as err:
        sys.stdout.write(f"bootstrap: {err}\n")
        return 2
    try:
        with lock.bootstrap_lock(lock.path(STATE, s.target, s.prefix)):
            return _run(a, real_ctx(s, a))
    except Busy as err:
        sys.stdout.write(f"bootstrap: {err}\n")
        return 75


def _run(a: argparse.Namespace, ctx: Ctx) -> int:
    ctx.needs_connect = not a.destroy and a.only != "preflight"
    if a.destroy or a.write_baseline or a.compare:
        try:
            preflight.check(ctx)
            if a.destroy:
                return destroy.destroy(ctx)
            return _baseline(a, ctx)
        except (Stop, Unknown) as err:
            ctx.say(f"bootstrap: {err}")
            return err.code if isinstance(err, Stop) else 1
    chosen = phases.PHASES[:1] if a.only == "preflight" else phases.PHASES
    rc = phases.drive(ctx, chosen)
    if (
        rc == 0
        and a.only is None
        and a.ledger_from
        and proof_record.release(ctx.state_dir, ctx.scope, a.ledger_from, a.seed_from)
    ):
        ctx.say(
            f"bootstrap: ledger from {a.ledger_from} restored; the proof run it names is closed"
        )
    return rc


def _baseline(a: argparse.Namespace, ctx: Ctx) -> int:
    from weather_signal.signal import train

    name = ctx.scope.names.table("signals", train.MODEL)

    def read(cutoff: date) -> Mapping[str, Any]:
        return baseline.write(ctx.sql, ctx.scope, cutoff, ctx.champion_tags(ctx.scope))

    if a.write_baseline:
        if a.cutoff is None:
            raise Stop(2, "--write-baseline needs --cutoff YYYY-MM-DD")
        a.write_baseline.write_text(json.dumps(read(a.cutoff), indent=1) + "\n", encoding="utf-8")
        ctx.say(f"baseline: wrote {a.write_baseline} for {name}")
        return 0
    diffs = baseline.compare(json.loads(a.compare.read_text(encoding="utf-8")), read)
    for d in diffs:
        ctx.say(f"compare: {d}")
    ctx.say("compare: equal to the baseline" if not diffs else f"compare: {len(diffs)} differences")
    return 1 if diffs else 0


def real_ctx(s: scope.Scope, a: argparse.Namespace) -> Ctx:
    from weather_signal.ingest.files import Files

    def say(text: str) -> None:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    return Ctx(
        scope=s,
        run=subprocess_runner(),
        sql=_connect_sql(s.profile),
        store=Files.from_sdk(s.profile),
        list_volume=list_volume_sdk(s.profile),
        setup=connect_setup,
        champion_tags=champion_tags,
        now=lambda: datetime.now(UTC),
        sleep=time.sleep,
        say=say,
        key_file=KEY_FILE,
        repo=Path.cwd(),
        seed_from=a.seed_from.expanduser() if a.seed_from else None,
        ledger_from=a.ledger_from.expanduser() if getattr(a, "ledger_from", None) else None,
        skip_app=a.skip_app,
        warehouse_name=a.warehouse_name,
        live=a.live,
        discover_catalog=a.b_catalog is None,
        argv=_own_args(sys.argv),
        state_dir=STATE,
    )


def _own_args(argv: list[str]) -> tuple[str, ...]:
    """What followed `bootstrap` on the command line, so a printed rerun repeats it."""
    return tuple(argv[argv.index("bootstrap") + 1 :]) if "bootstrap" in argv else ()


def _connect_sql(profile: str) -> Any:
    session: list[Any] = []

    def sql(q: str) -> list[dict[str, Any]]:
        if not session:
            from weather_signal.spark import spark_session

            session.append(spark_session(profile))
        try:
            return [r.asDict() for r in session[0].sql(q).collect()]
        except Exception as err:
            raise Unknown(f"SQL did not answer: {type(err).__name__}: {str(err)[:200]}") from err

    return sql


def connect_setup(s: scope.Scope, runner: str, agent: str, app_sp: str) -> None:
    """`setup` and its grants, then the App's SELECT on each view in the same phase."""
    from weather_signal.catalog import setup
    from weather_signal.catalog.ddl import BACKTEST_VIEW
    from weather_signal.guard.spark_catalog import SparkCatalog
    from weather_signal.serving.views import INTRADAY_VIEW, VIEWS
    from weather_signal.spark import spark_session

    n = s.names
    cat = SparkCatalog(spark_session(s.profile), n)
    setup.run(cat, n)
    for q in setup.grants(n, runner, agent):
        cat.execute(q)
    if app_sp:
        for v in (*VIEWS, BACKTEST_VIEW, INTRADAY_VIEW):
            cat.execute(f"GRANT SELECT ON TABLE {n.table('app', v)} TO `{app_sp}`")


def uc_client(profile: str) -> Any:
    """An MlflowClient on Unity Catalog through `profile`, both URIs given, so no default is read.

    Bare, mlflow 3.18 builds its registry from the local tracking URI and refuses (dev, 2026-11-19).
    """
    import mlflow

    return mlflow.MlflowClient(
        tracking_uri=f"databricks://{profile}", registry_uri=f"databricks-uc://{profile}"
    )


def champion_tags(s: scope.Scope) -> Mapping[str, str] | None:
    """`@champion`'s tags; with no alias, the newest version's when it records a failed gate."""
    from weather_signal.signal import train

    name, client = s.names.table("signals", train.MODEL), uc_client(s.profile)
    try:
        return dict(client.get_model_version_by_alias(name, "champion").tags)
    except Exception as err:
        if getattr(err, "error_code", None) != "RESOURCE_DOES_NOT_EXIST":
            raise Unknown(f"champion of {train.MODEL}: {type(err).__name__}") from err
    try:
        newest = train.MlflowRegistry.newest(client, name)
    except Exception as err:
        raise Unknown(f"versions of {train.MODEL}: {type(err).__name__}") from err
    return dict(newest[1]) if newest and newest[1].get("gate_pass") == "false" else None
