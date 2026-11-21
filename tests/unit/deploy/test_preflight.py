"""Preflight reads only, and refuses an owner step left undone with exit 2."""

import pytest

from weather_signal.deploy import preflight, scope
from weather_signal.deploy.cli import Result, Stop, Unknown


def refused(ctx):
    with pytest.raises(Stop) as e:
        preflight.check(ctx)
    return e.value


def test_a_ready_workspace_passes_and_names_its_warehouse(ctx, ws):
    preflight.check(ctx)
    assert ctx.warehouse_id == "wh-1" and ws.writes() == []


def test_not_signed_in_is_exit_2_with_the_login_line(ctx, ws):
    ws.broken.add("current-user me")
    assert "databricks auth login" in str(refused(ctx))


def test_an_old_cli_is_exit_2(ctx, ws):
    ws.rc["version"] = Result(0, "Databricks CLI v0.278.0\n", "")
    assert refused(ctx).code == 2


def test_a_missing_key_file_is_exit_2(ctx):
    ctx.key_file.unlink()
    assert "alphavantage.co" in str(refused(ctx))


def test_a_missing_catalog_is_exit_2_and_a_silent_one_unknown(ctx, ws):
    ws.rc["catalogs get"] = Result(1, "", "Error: Catalog 'workspace' does not exist.\n")
    assert refused(ctx).code == 2
    ws.rc["catalogs get"] = Result(1, "", "connection reset")
    with pytest.raises(Unknown):
        preflight.check(ctx)


@pytest.mark.parametrize("n", [0, 2])
def test_not_exactly_one_warehouse_is_exit_2(ctx, ws, n):
    ws.warehouses = [{"id": f"wh-{i}", "name": f"w{i}"} for i in range(n)]
    assert "--warehouse-name" in str(refused(ctx))


def test_a_named_warehouse_is_picked_from_several(ctx, ws):
    ws.warehouses = [{"id": "wh-1", "name": "a"}, {"id": "wh-2", "name": "b"}]
    ctx.warehouse_name = "b"
    preflight.check(ctx)
    assert ctx.warehouse_id == "wh-2"


def test_a_variable_overrides_file_is_exit_2(ctx, tmp_path):
    (tmp_path / "databricks.yml").write_text("", encoding="utf-8")
    f = tmp_path / ".databricks/bundle/dev/variable-overrides.json"
    f.parent.mkdir(parents=True)
    f.write_text("{}", encoding="utf-8")
    ctx.repo = tmp_path
    assert "variable-overrides.json" in str(refused(ctx))


def test_another_throwaway_prefix_alive_is_exit_2_with_its_destroy_line(ctx, ws):
    ctx.scope = scope.build("p", "scratch", "t2_", "workspace")
    ws.schemas += ["t1_signals", "t1_ops"]
    assert "--schema-prefix t1_ --destroy --yes" in str(refused(ctx))


def test_dev_ops_without_the_raw_schema_is_the_old_set(ctx, ws):
    ws.schemas = ["dev_ops", "dev_signals"]
    assert "old dev set" in str(refused(ctx))


def test_dev_state_recording_the_old_commit_is_exit_2(ctx, ws):
    ws.metadata = '{"git": {"commit": "9f88ad9e1"}}'
    assert "9f88ad9" in str(refused(ctx))


def test_dev_state_without_metadata_passes(ctx, ws):
    ws.metadata = None
    preflight.check(ctx)


def test_doesnt_exist_is_the_cli_wording_of_absent(ctx, ws):
    """`workspace export` of a missing path says "doesn't exist"."""
    from weather_signal.deploy.cli import not_found

    assert not_found(Result(1, "", "Error: Path (/Workspace/x) doesn't exist.\n"))
    assert not not_found(Result(1, "", "Error: connection reset"))


def test_no_data_folder_refuses_before_any_fetch(ctx):
    """Seed-only (owner, 2026-11-19): no folder, no run."""
    ctx.seed_from = None
    assert "--seed-from" in str(refused(ctx))
