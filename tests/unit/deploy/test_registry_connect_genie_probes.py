"""From the first live dev run (2026-11-19): the registry, Connect and the Genie list."""

import sys
import types

import pytest

from weather_signal.deploy import command, genie, phases, preflight, scope
from weather_signal.deploy.cli import Result


def only(*names):
    return tuple(p for p in phases.PHASES if p.name in names)


def test_the_champion_probe_addresses_unity_catalog_on_the_profile(monkeypatch):
    """Bare, MlflowClient read the local sqlite tracking URI and refused the registry."""
    seen = {}

    class Missing(Exception):
        error_code = "RESOURCE_DOES_NOT_EXIST"

    class Client:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs, args=args)

        def get_model_version_by_alias(self, name, alias):
            seen["asked"] = (name, alias)
            raise Missing()

        def search_model_versions(self, query):
            return []

    fake = types.SimpleNamespace(
        MlflowClient=Client, set_registry_uri=lambda u: seen.setdefault("global", u)
    )
    monkeypatch.setitem(sys.modules, "mlflow", fake)
    s = scope.build("p", "dev", None, "workspace")
    assert command.champion_tags(s) is None
    assert seen["tracking_uri"] == "databricks://p" and seen["registry_uri"] == "databricks-uc://p"
    assert seen["asked"] == ("workspace.dev_signals.stock_signal", "champion")
    assert "global" not in seen  # nothing process-wide is set


@pytest.mark.parametrize(("gate_pass", "found"), [("false", True), ("true", False)])
def test_with_no_alias_a_newest_version_that_failed_its_gate_is_the_state(
    monkeypatch, gate_pass, found
):
    """A failed gate leaves no alias; its version's tags still answer the train phase."""

    class Missing(Exception):
        error_code = "RESOURCE_DOES_NOT_EXIST"

    tags = {"fit": phases.TRAIN_FIT, "fit_as_of": "2026-01-02T21:30:00Z", "gate_pass": gate_pass}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def get_model_version_by_alias(self, name, alias):
            raise Missing()

        def search_model_versions(self, query):
            return [types.SimpleNamespace(version="3"), types.SimpleNamespace(version="12")]

        def get_model_version(self, name, version):
            return types.SimpleNamespace(tags=tags if version == "12" else {})

    monkeypatch.setitem(sys.modules, "mlflow", types.SimpleNamespace(MlflowClient=Client))
    got = command.champion_tags(scope.build("p", "dev", None, "workspace"))
    assert got == (tags if found else None)


def test_a_run_without_databricks_connect_stops_at_preflight_naming_the_venv(ctx, ws):
    ctx.importable = lambda m: m != "databricks.connect"
    assert phases.drive(ctx) == 2
    assert ".venv-connect" in ctx.lines[-1]
    assert ws.writes() == []


def test_preflight_alone_notes_the_missing_connect_and_goes_on(ctx):
    ctx.importable = lambda m: m != "databricks.connect"
    ctx.needs_connect = False
    preflight.check(ctx)
    assert any(".venv-connect" in x and "note" in x for x in ctx.lines)


def test_a_new_space_that_lists_a_few_seconds_late_is_found_by_its_id(ctx, ws):
    ws.spaces, ws.list_lag = [], 3
    slept = []
    ctx.sleep = slept.append
    assert phases.drive(ctx, only("genie")) == 0
    assert len(ws.created) == 1 and slept == []


def test_a_space_that_never_lists_stops_after_a_bounded_wait(ctx, ws):
    """A create that returned no id has only the list to find it by."""
    ws.spaces, ws.list_lag = [], 10_000
    real = ws._answer
    ws._answer = lambda v, a: Result(0, "{}", "") if v == "genie create-space" else real(v, a)
    slept = []
    ctx.sleep = slept.append
    assert phases.drive(ctx, only("genie")) == 1
    assert sum(slept) <= genie.LIST_WAIT_S + genie.LIST_EVERY_S
    assert "not listed" in ctx.lines[-1]
