"""deploy-hardening: an interrupted prod rerun, an interrupted secret write, the worktree pin, Genie."""

import importlib
import json
from datetime import timedelta

import pytest

from tests.unit.deploy.conftest import FAKE_SECRET, HOST, REPO
from tests.unit.deploy.test_agent_phase import _creates, fresh_mac  # noqa: F401
from tests.unit.deploy.test_rerun_keeps_pause import JOBS, prod  # noqa: F401
from weather_signal.deploy import agent, genie, phases, preflight, schedules, scope
from weather_signal.deploy.cli import Result
from weather_signal.deploy.context import Ctx


def _linked_main():
    return importlib.import_module("weather_signal.deploy.checkout").linked_main


def only(*names):
    return tuple(p for p in phases.PHASES if p.name in names)


def _deletes(ws):
    return [c for c in ws.calls if c[1:3] == ["service-principal-secrets-proxy", "delete"]]


# 1. A live prod stopped between the paused deploy and `live` is live after the rerun.


def _new_process(c):
    c.was_live, c.pause = None, "PAUSED"


def test_a_live_prod_stopped_after_its_paused_deploy_ends_live_on_the_rerun(prod, ws):  # noqa: F811
    ws.jobs = dict.fromkeys(JOBS, "UNPAUSED")
    assert phases.drive(prod, only("bundle")) == 0  # the run stops here, before `live`
    assert ws.deployed["pause"] == "PAUSED" and schedules.held(prod)
    ws.jobs = dict.fromkeys(JOBS, "PAUSED")  # what the workspace reports to the rerun
    _new_process(prod)
    assert phases.drive(prod, only("bundle", "live")) == 0
    assert ws.deployed["pause"] == "UNPAUSED"
    assert not schedules.held(prod)


def test_the_hold_is_written_before_the_deploy_so_a_failed_deploy_keeps_it(prod, ws):  # noqa: F811
    ws.jobs = dict.fromkeys(JOBS, "UNPAUSED")
    ws.rc["bundle deploy"] = Result(1, "", "Error: boom\n")
    assert phases.drive(prod, only("bundle")) == 1
    assert schedules.held(prod)


def test_a_paused_prod_with_no_hold_still_stays_paused(prod, ws):  # noqa: F811
    ws.jobs = dict.fromkeys(JOBS, "PAUSED")
    ws.deployed["pause"] = "PAUSED"
    assert phases.drive(prod, only("bundle", "live")) == 0
    assert ws.deployed["pause"] == "PAUSED" and not schedules.held(prod)


def test_scratch_never_holds(ctx, ws):
    ctx.scope = scope.build("p", "scratch", "t9_", "workspace")
    ws.plan = {"plan": {"resources.jobs.score": {"action": "update"}}}
    phases.bundle_run(ctx)
    assert not schedules.held(ctx)


# 2. Ctrl-C between the create and the rename deletes the new secret.


@pytest.fixture
def interrupted(fresh_mac, monkeypatch):  # noqa: F811
    real, hits = agent.os.replace, []

    def replace(src, dst):
        if not hits:
            hits.append(dst)
            raise KeyboardInterrupt
        return real(src, dst)

    monkeypatch.setattr(agent.os, "replace", replace)
    return fresh_mac


def test_ctrl_c_after_the_create_deletes_the_secret_and_reraises(interrupted, ws):
    with pytest.raises(KeyboardInterrupt):
        phases.drive(interrupted, only("agent"))
    assert len(_creates(ws)) == 1
    assert [c[3:5] for c in _deletes(ws)] == [["12", "s-1"]]
    assert "[weather-signal-agent]" not in interrupted.cfg_file.read_text(encoding="utf-8")
    assert not any(FAKE_SECRET in line for line in interrupted.lines)
    assert not list(interrupted.cfg_file.parent.glob("*.weather-signal.tmp"))


def test_the_rerun_after_ctrl_c_leaves_exactly_one_secret(interrupted, ws):
    with pytest.raises(KeyboardInterrupt):
        phases.drive(interrupted, only("agent"))
    assert phases.drive(interrupted, only("agent")) == 0
    assert len(_creates(ws)) - len(_deletes(ws)) == 1


def test_a_profile_written_during_the_create_survives(fresh_mac, ws):  # noqa: F811
    """Another writer (an `auth login`) between the read and the rename keeps its profile."""
    real = ws._answer

    def answer(verb, a):
        if verb == "service-principal-secrets-proxy create":
            with fresh_mac.cfg_file.open("a", encoding="utf-8") as f:
                f.write(f"\n[other]\nhost = {HOST}\n")
        return real(verb, a)

    ws._answer = answer
    assert phases.drive(fresh_mac, only("agent")) == 0
    text = fresh_mac.cfg_file.read_text(encoding="utf-8")
    assert "[other]" in text and f"client_secret = {FAKE_SECRET}" in text
    assert text.count("[weather-signal-agent]") == 1


# 3. Bootstrap refuses to install the LaunchAgent from a linked worktree.


@pytest.fixture
def worktree(tmp_path):
    main, wt = tmp_path / "main", tmp_path / "main/.worktrees/x"
    (main / ".git/worktrees/x").mkdir(parents=True)
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/x\n", encoding="utf-8")
    (wt / "databricks.yml").write_text("bundle: {}\n", encoding="utf-8")
    return main, wt


def test_a_linked_worktree_names_its_main_checkout(worktree):
    main, wt = worktree
    assert _linked_main()(wt) == main
    assert _linked_main()(main) is None
    assert _linked_main()(REPO.parent / "no-such-dir") is None


def test_a_dev_run_from_a_worktree_stops_at_preflight_naming_the_main_checkout(ctx, ws, worktree):
    main, wt = worktree
    ctx.repo, ctx.linked_main = wt, _linked_main()
    assert phases.drive(ctx, only("preflight", "agent")) == 2
    assert "linked worktree" in ctx.lines[-1] and str(main) in ctx.lines[-1]
    assert ws.calls == []


def test_a_scratch_run_from_a_worktree_is_not_refused(ctx, ws, worktree):
    _, wt = worktree
    ctx.scope = scope.build("p", "scratch", "t9_", "workspace")
    ctx.repo, ctx.linked_main = wt, _linked_main()
    preflight.check(ctx)
    assert ws.calls


def test_a_dev_run_from_the_main_checkout_passes_the_check(ctx, worktree):
    main, _ = worktree
    ctx.repo, ctx.linked_main = main, _linked_main()
    preflight._main_checkout(ctx)


# 4. A rerun inside the Genie list lag finds the space it made and makes no second one.


def test_a_rerun_inside_the_list_lag_creates_no_second_space(ctx, ws):
    ws.spaces, ws.list_lag = [], 10_000
    ctx.warehouse_id = "wh-1"
    phases.drive(ctx, only("genie"))
    ctx.lines.clear()
    assert phases.drive(ctx, only("genie")) == 0
    assert len(ws.created) == 1 and ctx.lines == ["genie: done"]


def test_a_record_past_the_lag_defers_to_the_list(ctx, ws):
    """A space trashed by hand after its create must not count once the list is the truth."""
    ws.spaces, ws.list_lag = [], 10_000
    phases.drive(ctx, only("genie"))
    later = ctx.now() + timedelta(seconds=genie.RECORD_TRUST_S + 1)
    ctx.now = lambda: later
    assert genie.found(ctx) == []
    assert not (ctx.state_dir / "genie-dev-dev_.json").exists()


def test_a_recorded_space_the_workspace_no_longer_has_is_created_again(ctx, ws):
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    doc = {"space_id": "g-gone", "created_at": ctx.now().isoformat()}
    (ctx.state_dir / "genie-dev-dev_.json").write_text(json.dumps(doc), encoding="utf-8")
    ws.spaces = []
    assert phases.drive(ctx, only("genie")) == 0
    assert len(ws.created) == 1


def test_a_get_space_that_does_not_answer_creates_nothing(ctx, ws):
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    doc = {"space_id": "g-7", "created_at": ctx.now().isoformat()}
    (ctx.state_dir / "genie-dev-dev_.json").write_text(json.dumps(doc), encoding="utf-8")
    ws.spaces, ws.broken = [], {"genie get-space"}
    assert phases.drive(ctx, only("genie")) == 1
    assert ws.created == []


# The default importable answers False when the parent package is absent.


def test_importable_is_false_for_a_module_whose_package_is_absent():
    default = Ctx.__dataclass_fields__["importable"].default
    assert default("no_such_package_xyz.connect") is False
    assert default("json") is True
