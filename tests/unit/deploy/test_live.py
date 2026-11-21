"""Phase live: the build deploys every schedule paused, and only the last phase unpauses them."""

import pytest

from weather_signal.deploy import phases, scope
from weather_signal.deploy.cli import Result


def only(*names):
    return tuple(p for p in phases.PHASES if p.name in names)


def _deploys(ws):
    return [c for c in ws.calls if c[1:3] == ["bundle", "deploy"]]


def _pause(argv):
    return [a for a in argv if a.startswith("schedule_pause=")]


@pytest.fixture
def changing(ws):
    """A plan that changes until one deploy, then lists no change."""
    ws.plan = {"plan": {"resources.jobs.score": {"action": "update"}}}
    real = ws._answer

    def answer(verb, a):
        if verb == "bundle deploy":
            ws.plan = {"plan": {"resources.jobs.score": {"action": "skip"}}}
        return real(verb, a)

    ws._answer = answer
    return ws


def test_live_is_the_last_project_phase_and_agent_the_one_before():
    assert [p.name for p in phases.PHASES][-3:] == ["agent", "live", "gitfolder"]


def test_the_bundle_phase_deploys_paused(ctx, changing):
    assert phases.drive(ctx, only("bundle")) == 0
    assert _pause(_deploys(changing)[0]) == ["schedule_pause=PAUSED"]


def test_dev_goes_live_without_a_flag(ctx, changing):
    assert phases.drive(ctx, only("live")) == 0
    assert _pause(_deploys(changing)[0]) == ["schedule_pause=UNPAUSED"]
    plans = [c for c in changing.calls if c[1:3] == ["bundle", "plan"]]
    assert all(_pause(c) == ["schedule_pause=UNPAUSED"] for c in plans)


def test_prod_goes_live_only_with_live(ctx, changing):
    ctx.scope = scope.build("p", "prod", None, "workspace")
    assert phases.drive(ctx, only("live")) == 0
    assert _deploys(changing) == [] and ctx.lines[-1] == "live: done"
    ctx.live = True
    assert phases.drive(ctx, only("live")) == 0
    assert _pause(_deploys(changing)[0]) == ["schedule_pause=UNPAUSED"]


def test_scratch_never_goes_live(ctx, changing):
    ctx.scope = scope.build("p", "scratch", "t1_", "workspace")
    ctx.live = True
    assert phases.drive(ctx, only("live")) == 0
    assert _deploys(changing) == []


def test_a_rerun_over_a_live_set_neither_pauses_nor_deploys(ctx, ws):
    """The plan differs only by the pause variable: with UNPAUSED it lists no change."""
    real = ws._answer

    def answer(verb, a):
        if verb == "bundle plan" and "schedule_pause=PAUSED" in a:
            return Result(0, '{"plan": {"resources.jobs.score": {"action": "update"}}}', "")
        return real(verb, a)

    ws._answer = answer
    assert phases.drive(ctx, only("bundle", "live")) == 0
    assert _deploys(ws) == []
