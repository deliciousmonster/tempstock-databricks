"""`bundle plan -o json` as recorded from CLI 1.26.0 on a fresh t9_ scope, and the default-deny guard."""

import json
from pathlib import Path

import pytest

from weather_signal.deploy import plan
from weather_signal.deploy.cli import Stop, Unknown

FIXTURES = Path(__file__).parent / "fixtures"


def test_the_recorded_plan_parses_as_all_create():
    doc = json.loads((FIXTURES / "plan-scratch.json").read_text(encoding="utf-8"))
    got = plan.changes(doc)
    assert got and {p.action for p in got} == {"create"}
    assert len(got) == len(doc["plan"])


def test_skip_is_no_change():
    assert plan.changes({"plan": {"resources.jobs.a": {"action": "skip"}}}) == []


@pytest.mark.parametrize("action", ["delete", "recreate"])
@pytest.mark.parametrize(
    "key",
    [
        "resources.schemas.raw",
        "resources.volumes.raw",
        "resources.apps.ui_app",
        "resources.registered_models.stock_signal",
        "resources.secret_scopes.alphavantage",
        "resources.schemas.signals.grants",
        "resources.pipelines.weather_signal",
    ],
)
def test_destroying_anything_but_a_job_is_denied(key, action):
    with pytest.raises(Stop) as e:
        plan.changes({"plan": {key: {"action": action}}})
    assert e.value.code == 1 and key in str(e.value)


def test_a_job_may_be_recreated():
    got = plan.changes({"plan": {"resources.jobs.score": {"action": "recreate"}}})
    assert [p.action for p in got] == ["recreate"]


@pytest.mark.parametrize(
    "doc",
    [
        {},
        {"plan": None},
        {"plan": {"resources.jobs.a": {"action": "replace"}}},
        {"plan": {"resources.jobs.a": {}}},
        {"plan": {"jobs.a": {"action": "delete"}}},
    ],
)
def test_anything_unrecognized_is_unknown(doc):
    with pytest.raises(Unknown):
        plan.changes(doc)
