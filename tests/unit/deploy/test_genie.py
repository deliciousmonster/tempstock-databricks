"""The Genie space comes after `setup`, by bootstrap, never in the first deploy (measured 403, 2026-11-19)."""

import json
from pathlib import Path

import pytest

from tests.unit.deploy.conftest import REPO
from weather_signal.deploy import destroy, genie, phases, scope
from weather_signal.deploy.cli import Result, Stop
from weather_signal.serving import views


def only(*names):
    return tuple(p for p in phases.PHASES if p.name in names)


def test_the_space_is_made_after_setup_creates_the_views():
    names = [p.name for p in phases.PHASES]
    assert names.index("bundle") < names.index("setup") < names.index("genie")


def test_a_workspace_without_the_space_gets_one_over_the_app_views(ctx, ws):
    ctx.warehouse_id = "wh-1"
    ws.spaces = []
    assert phases.drive(ctx, only("genie")) == 0
    assert len(ws.created) == 1
    body = ws.created[0]
    assert body["title"] == "dev_weather-signal" and body["warehouse_id"] == "wh-1"
    tables = [
        t["identifier"] for t in json.loads(body["serialized_space"])["data_sources"]["tables"]
    ]
    assert tables == [f"workspace.dev_app.{v}" for v in sorted(views.VIEWS)]
    assert "${" not in body["serialized_space"]


def test_a_workspace_with_the_space_gets_no_second_one(ctx, ws):
    assert phases.drive(ctx, only("genie")) == 0
    assert ws.created == [] and ctx.lines[-1] == "genie: done"


def test_a_list_that_does_not_answer_stops_before_any_create(ctx, ws):
    ws.spaces = []
    ws.broken.add("genie list-spaces")
    assert phases.drive(ctx, only("genie")) == 1
    assert ws.writes() == []


def test_a_refused_create_names_the_cli_error(ctx, ws):
    ws.spaces = []
    ws.rc["genie create-space"] = Result(1, "", "Error: Table 'x' does not exist\n")
    assert phases.drive(ctx, only("genie")) == 1
    assert "Table 'x' does not exist" in ctx.lines[-1]


def test_destroy_trashes_the_scratch_space_it_made(ctx, ws):
    ctx.scope = scope.build("p", "scratch", "t9_", "workspace")
    ws.spaces = [
        {"space_id": "g-9", "title": "t9_weather-signal", "description": genie.DESCRIPTION},
        {"space_id": "g-1", "title": "x"},
    ]
    ws.rc["genie trash-space"] = Result(0, "", "")
    destroy.destroy(ctx)
    trashed = [a[3] for a in ws.writes() if a[1:3] == ["genie", "trash-space"]]
    assert trashed == ["g-9"]


def test_destroy_never_trashes_a_same_titled_space_bootstrap_did_not_make(ctx, ws):
    """The title alone took another owner's space; trash now needs DESCRIPTION too."""
    ctx.scope = scope.build("p", "scratch", "t9_", "workspace")
    ws.spaces = [
        {"space_id": "g-8", "title": "t9_weather-signal", "description": genie.DESCRIPTION},
        {"space_id": "g-9", "title": "t9_weather-signal", "description": "someone else's"},
    ]
    ws.rc["genie trash-space"] = Result(0, "", "")
    with pytest.raises(Stop, match="g-9 titled t9_weather-signal but bootstrap did not make it"):
        destroy.destroy(ctx)
    assert [a for a in ws.writes() if a[1:3] == ["genie", "trash-space"]] == []


def test_space_json_renders_into_any_scope():
    text = genie.serialized(Path(REPO), "cat.t9_app")
    tables = [t["identifier"] for t in json.loads(text)["data_sources"]["tables"]]
    assert all(t.startswith("cat.t9_app.v_ui_") for t in tables)


def _stale(ws):
    doc = json.loads(ws.spaces[0]["serialized_space"])
    doc["instructions"]["text_instructions"][0]["content"] = ["an older instruction\n"]
    ws.spaces[0]["serialized_space"] = json.dumps(doc)


def test_an_existing_space_with_an_older_instruction_is_updated_in_place(ctx, ws):
    _stale(ws)
    assert phases.drive(ctx, only("genie")) == 0
    assert ws.created == [] and ctx.lines[-1] == "genie: ran"
    [(sid, body)] = ws.updated
    assert sid == "g-1"
    assert body == {"serialized_space": genie.serialized(Path(REPO), "workspace.dev_app")}


def test_a_current_space_is_neither_created_nor_updated(ctx, ws):
    assert phases.drive(ctx, only("genie")) == 0
    assert ws.writes() == [] and ctx.lines[-1] == "genie: done"


def test_a_get_space_that_does_not_answer_stops_before_any_update(ctx, ws):
    _stale(ws)
    ws.broken.add("genie get-space")
    assert phases.drive(ctx, only("genie")) == 1
    assert ws.writes() == []


def test_a_refused_update_names_the_cli_error(ctx, ws):
    _stale(ws)
    ws.rc["genie update-space"] = Result(1, "", "Error: PERMISSION_DENIED\n")
    assert phases.drive(ctx, only("genie")) == 1
    assert "update-space g-1 failed" in ctx.lines[-1] and "PERMISSION_DENIED" in ctx.lines[-1]


def test_a_same_titled_space_bootstrap_did_not_make_is_never_replaced(ctx, ws):
    """A title match alone once let update-space replace another's space."""
    _stale(ws)
    ws.spaces[0]["description"] = "someone else's space"
    assert phases.drive(ctx, only("genie")) == 1
    assert ws.writes() == [] and "bootstrap did not make" in ctx.lines[-1]
