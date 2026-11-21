"""Each code change deploys under a new artifacts/<tag>; after a deploy the bundle's own older tag
folders go, and nothing outside that path does (bootstrap-resume review r1)."""

import pytest

from weather_signal.deploy import phases, scope
from weather_signal.deploy.cli import Result

USER = "/Workspace/Users/owner@example.com/.bundle/weather-signal"
NOW_TAG, OLD, OLDER = "0123456789ab", "ba9876543210", "aaaaaaaaaaaa"
# modified_at (ms): OLDER is the newest other folder, though its name sorts first.
AGES = {NOW_TAG: 400, OLDER: 300, OLD: 200, "manual": 100, ".internal": 50, "notes": 50}


def world(root: str, *, api_prefix: str = "/Workspace") -> dict[str, str]:
    base = api_prefix + root.removeprefix("/Workspace")
    return {
        f"{base}/{NOW_TAG}": "DIRECTORY",
        f"{base}/{NOW_TAG}/.internal": "DIRECTORY",
        f"{base}/{OLD}": "DIRECTORY",
        f"{base}/{OLD}/.internal": "DIRECTORY",
        f"{base}/{OLDER}": "DIRECTORY",
        f"{base}/manual": "DIRECTORY",
        f"{base}/.internal": "DIRECTORY",  # the untagged upload before tags; not a tag folder
        f"{base}/cafecafecafe": "FILE",
        f"{base}/notes": "DIRECTORY",
    }


def rows(held, ages=AGES):
    """The fake world's listing rows: each path's object_type, and its modified_at by name."""
    name = lambda k: k.rsplit("/", 1)[1]  # noqa: E731
    return {k: {"object_type": t} | ({"modified_at": ages[name(k)]} if name(k) in ages else {})
            for k, t in held.items()}  # fmt: skip


def deployed(ctx, ws, held, ages=AGES):
    ctx.wheel_tag = NOW_TAG
    ws.artifacts = rows(held, ages)
    phases.bundle_run(ctx)
    return [c[3] for c in ws.writes() if c[1:3] == ["workspace", "delete"]]


def test_a_deploy_removes_the_older_tag_folders_under_its_own_artifacts_path(ctx, ws):
    root = f"{USER}/dev/artifacts"
    prod = {f"{USER}/prod/artifacts/{OLD}": "DIRECTORY"}
    deleted = deployed(ctx, ws, {**world(root), **prod})
    assert deleted == [f"{root}/{OLD}", f"{root}/manual"]
    left = set(ws.artifacts)
    assert {f"{root}/{NOW_TAG}", f"{root}/{NOW_TAG}/.internal", f"{root}/.internal",
            f"{root}/{OLDER}", f"{root}/cafecafecafe", f"{root}/notes", *prod} == left  # fmt: skip
    assert f"artifacts: removed {root}/{OLD}" in ctx.lines


def test_a_listing_without_the_workspace_prefix_names_the_same_folders(ctx, ws):
    root = f"{USER}/dev/artifacts"
    deleted = deployed(ctx, ws, world(root, api_prefix=""))
    assert deleted == [f"{root}/{OLD}", f"{root}/manual"]


def test_a_scratch_deploy_prunes_only_its_own_prefix(ctx, ws):
    ctx.scope = scope.build("p", "scratch", "t3_", "workspace")
    mine, other = f"{USER}/scratch-t3_/artifacts", f"{USER}/scratch-t4_/artifacts"
    deleted = deployed(ctx, ws, {**world(mine), **world(other)})
    assert deleted == [f"{mine}/{OLD}", f"{mine}/manual"]
    assert all(k in ws.artifacts for k in world(other))


def test_nothing_is_removed_when_the_listing_lacks_this_deploys_folder(ctx, ws):
    root = f"{USER}/dev/artifacts"
    held = {k: v for k, v in world(root).items() if NOW_TAG not in k}
    assert deployed(ctx, ws, held) == []
    assert any("old upload folders left" in x for x in ctx.lines)


@pytest.mark.parametrize("answer", [Result(1, "", "Error: 500"), Result(0, "not json", "")])
def test_a_listing_that_does_not_answer_removes_nothing(ctx, ws, answer):
    ws.rc["workspace list"] = answer
    assert deployed(ctx, ws, world(f"{USER}/dev/artifacts")) == []
    assert any("old upload folders left" in x for x in ctx.lines)


def test_the_newest_other_tag_folder_is_kept_for_a_run_started_before_the_deploy(ctx, ws):
    root = f"{USER}/dev/artifacts"
    held = {f"{root}/{NOW_TAG}": "DIRECTORY", f"{root}/{OLD}": "DIRECTORY"}
    assert deployed(ctx, ws, held) == []
    held[f"{root}/{OLDER}"] = "DIRECTORY"
    assert deployed(ctx, ws, held) == [f"{root}/{OLD}"]


def test_a_tag_folder_without_an_age_removes_nothing(ctx, ws):
    ages = {NOW_TAG: 400, OLDER: 300, "manual": 100}
    assert deployed(ctx, ws, world(f"{USER}/dev/artifacts"), ages) == []
    assert any("has no modified_at" in x for x in ctx.lines)


def test_prod_is_never_pruned(ctx, ws):
    ctx.scope = scope.build("p", "prod", None, "workspace")
    assert deployed(ctx, ws, world(f"{USER}/prod/artifacts")) == []
    assert [c for c in ws.calls if c[1:3] == ["workspace", "list"]] == []


def test_a_hand_tag_removes_nothing(ctx, ws):
    ws.artifacts = rows(world(f"{USER}/dev/artifacts"))
    ctx.wheel_tag = "manual"
    phases.bundle_run(ctx)
    assert [c for c in ws.writes() if c[1:3] == ["workspace", "delete"]] == []


def test_a_failed_deploy_lists_and_removes_nothing(ctx, ws):
    ws.rc["bundle deploy"] = Result(1, "", "Error: boom")
    with pytest.raises(phases.Stop):
        deployed(ctx, ws, world(f"{USER}/dev/artifacts"))
    assert [c for c in ws.calls if c[1:3] in (["workspace", "list"], ["workspace", "delete"])] == []
