"""Preflight for a fresh clone: one command for the Connect venv, the catalog found, not assumed."""

from weather_signal.deploy import phases, preflight
from weather_signal.deploy.cli import Result


def only(*names):
    return tuple(p for p in phases.PHASES if p.name in names)


def test_without_connect_it_prints_one_command_that_builds_the_venv_and_reruns(ctx, ws):
    ctx.importable = lambda m: m != "databricks.connect"
    ctx.argv = ("--profile", "p", "--target", "dev", "--seed-from", "/data")
    assert phases.drive(ctx, only("preflight")) == 2
    line = ctx.lines[-1]
    assert line.count("uv ") == 1 and "uv sync" not in line and "--no-sync" not in line
    assert line.endswith(
        "run: UV_PROJECT_ENVIRONMENT=.venv-connect uv run --no-default-groups --group connect "
        "--extra agent weather-signal bootstrap --profile p --target dev --seed-from /data"
    )
    assert ws.writes() == []


def test_with_no_catalog_given_the_metastore_default_is_used(ctx, ws):
    ctx.discover_catalog = True
    ws.rc["metastores current"] = Result(0, '{"default_catalog_name": "main"}', "")
    preflight.check(ctx)
    assert ctx.scope.catalog == "main"
    assert ["databricks", "catalogs", "get", "main"] == next(
        c[:4] for c in ws.calls if c[1:3] == ["catalogs", "get"]
    )


def test_a_given_catalog_is_not_looked_up(ctx, ws):
    preflight.check(ctx)
    assert not any(c[1:3] == ["metastores", "current"] for c in ws.calls)
    assert ctx.scope.catalog == "workspace"


def test_an_unplain_default_catalog_is_unknown(ctx, ws):
    ctx.discover_catalog = True
    ws.rc["metastores current"] = Result(0, '{"default_catalog_name": "a;DROP"}', "")
    assert phases.drive(ctx, only("preflight")) == 1
