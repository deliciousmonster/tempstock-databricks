"""The gitfolder phase: a Git folder of the configured URL in the user's home, kept on the branch."""

import pytest

from tests.unit.deploy.conftest import HEAD
from weather_signal import config
from weather_signal.deploy import phases

PATH = "/Workspace/Users/owner@example.com/" + config.GIT_FOLDER_URL.rsplit("/", 1)[1].removesuffix(".git")


def gitfolder():
    return tuple(p for p in phases.PHASES if p.name == "gitfolder")


def repo_writes(ws):
    return [c[1:4] for c in ws.writes() if c[1] == "repos"]


def test_an_absent_folder_is_created_at_the_users_home_and_checked_out_to_main(ctx, ws):
    ws.git_folder = None
    assert phases.drive(ctx, gitfolder()) == 0
    create, update = [c for c in ws.calls if c[1] == "repos" and c[2] in ("create", "update")]
    assert create[3:6] == [config.GIT_FOLDER_URL, "gitHub", "--path"] and create[6] == PATH
    assert update[2:6] == ["update", "78", "--branch", "main"]
    assert ctx.lines[-1] == "gitfolder: ran"


def test_a_folder_on_main_at_the_remote_head_gets_no_write(ctx, ws):
    assert phases.drive(ctx, gitfolder()) == 0
    assert ws.writes() == [] and ctx.lines == ["gitfolder: done"]


@pytest.mark.parametrize(("branch", "head"), [("main", "0ld"), ("feature", HEAD)])
def test_a_folder_behind_or_on_another_branch_is_updated_never_recreated(ctx, ws, branch, head):
    ws.git_folder.update(branch=branch, head_commit_id=head)
    assert phases.drive(ctx, gitfolder()) == 0
    assert repo_writes(ws) == [["repos", "update", "77"]]
    assert ws.git_folder["branch"] == "main" and ws.git_folder["head_commit_id"] == HEAD


def test_no_git_credential_skips_and_bootstrap_goes_on(ctx, ws):
    ws.git_credentials = []
    ws.git_folder = None
    assert phases.drive(ctx, gitfolder()) == 0
    assert ws.writes() == []
    assert ctx.lines[0].startswith("gitfolder: skipped: the deploying user has no GitHub")


def test_a_remote_that_does_not_answer_skips_and_bootstrap_goes_on(ctx, ws):
    ws.remote_head = None
    assert phases.drive(ctx, gitfolder()) == 0
    assert ws.writes() == []
    assert ctx.lines[0].startswith("gitfolder: skipped:") and "git ls-remote" in ctx.lines[0]


def test_something_else_at_the_path_stops_the_phase_and_is_left_alone(ctx, ws):
    ws.git_folder = {"object_type": "DIRECTORY", "id": 5}
    assert phases.drive(ctx, gitfolder()) == 1
    assert ws.writes() == []
    assert (
        ctx.lines[-1]
        == f"gitfolder: stopped: {PATH} is a DIRECTORY, not a Git folder; move it aside"
    )


def test_a_git_folder_of_another_url_stops_the_phase_and_is_left_alone(ctx, ws):
    ctx.git_folder_url = "https://github.com/example/" + PATH.rsplit("/", 1)[1] + ".git"
    assert phases.drive(ctx, gitfolder()) == 1
    assert ws.writes() == []
    assert (
        f"{PATH} is a Git folder of {config.GIT_FOLDER_URL}, not {ctx.git_folder_url}"
        in ctx.lines[-1]
    )


def test_the_same_url_without_dot_git_is_the_same_folder(ctx, ws):
    ws.git_folder["url"] = config.GIT_FOLDER_URL.removesuffix(".git")
    assert phases.drive(ctx, gitfolder()) == 0
    assert ws.writes() == [] and ctx.lines == ["gitfolder: done"]


def test_the_url_and_branch_are_read_from_config_alone(ctx, ws):
    ctx.git_folder_branch = "release"
    phases.drive(ctx, gitfolder())
    ls_remote = next(c for c in ws.calls if c[0] == "env")
    assert ls_remote[-2:] == [config.GIT_FOLDER_URL, "refs/heads/release"]
    assert "GIT_TERMINAL_PROMPT=0" in ls_remote
