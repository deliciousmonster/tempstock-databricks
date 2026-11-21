"""A prod rerun without --live keeps the pause state prod has deployed."""

import pytest

from tests.unit.deploy.conftest import REPO, ok
from tests.unit.deploy.test_live import _deploys, _pause, only
from weather_signal.deploy import phases, scope

JOBS = (
    "weather-signal-ingest",
    "weather-signal-score",
    "weather-signal-train",
    "weather-signal-ui-snapshot",
)


def _var(argv):
    return _pause(argv)[0].removeprefix("schedule_pause=")


@pytest.fixture
def prod(ctx, ws):
    """Prod run without --live; its plan differs while a real change or the pause variable does."""
    ctx.scope = scope.build("p", "prod", None, "workspace")
    ws.deployed = {"pause": "UNPAUSED", "changed": True}
    real = ws._answer

    def answer(verb, a):
        d = ws.deployed
        if verb == "bundle plan":
            action = "update" if d["changed"] or _var(a) != d["pause"] else "skip"
            return ok({"plan": {"resources.jobs.score": {"action": action}}})
        if verb == "bundle deploy":
            d.update(changed=False, pause=_var(a))
            ws.jobs = dict.fromkeys(ws.jobs, _var(a))  # a deploy sets every schedule it holds
        return real(verb, a)

    ws._answer = answer
    return ctx


def test_the_scheduled_jobs_are_read_from_the_resources():
    from weather_signal.deploy import schedules

    assert sorted(schedules.scheduled_jobs(REPO)) == sorted(JOBS)


def test_a_live_prod_rerun_without_live_ends_unpaused(prod, ws):
    ws.jobs = dict.fromkeys(JOBS, "UNPAUSED")
    assert phases.drive(prod, only("bundle", "live")) == 0
    assert ws.deployed["pause"] == "UNPAUSED"
    assert "stay live" in " ".join(prod.lines)


def test_a_live_prod_with_no_change_deploys_nothing(prod, ws):
    ws.jobs = dict.fromkeys(JOBS, "UNPAUSED")
    ws.deployed["changed"] = False
    assert phases.drive(prod, only("bundle", "live")) == 0
    assert _deploys(ws) == []


def test_a_paused_prod_rerun_without_live_stays_paused(prod, ws):
    ws.jobs = dict.fromkeys(JOBS, "PAUSED")
    ws.deployed["pause"] = "PAUSED"
    assert phases.drive(prod, only("bundle", "live")) == 0
    assert all(_pause(d) == ["schedule_pause=PAUSED"] for d in _deploys(ws))
    assert prod.lines[-2] == "live: schedules stay paused on prod"


def test_mixed_deployed_schedules_are_refused_before_any_deploy(prod, ws):
    ws.jobs = {**dict.fromkeys(JOBS, "UNPAUSED"), JOBS[0]: "PAUSED"}
    assert phases.drive(prod, only("bundle", "live")) == 2
    assert _deploys(ws) == [] and "disagree" in prod.lines[-1]


def test_an_unanswered_jobs_list_deploys_nothing(prod, ws):
    ws.broken.add("jobs list")
    assert phases.drive(prod, only("bundle", "live")) == 1
    assert _deploys(ws) == []
