"""Live agent tests on this Mac: real CLI turns (budgeted in each test's docstring), Connect on `dev` only."""

import os

import pytest

pytest.importorskip("claude_agent_sdk", reason="needs the agent extra")

TEST_SERVICE = "workspace.default.claude_subscription"
GATEWAY_ENV = {
    "CREW_LLM_BACKEND": "plan",
    "CREW_LLM_ROUTE": "databricks-gateway",
    "CREW_DATABRICKS_PROFILE": "weather-signal",
    "CREW_DATABRICKS_SERVICE": TEST_SERVICE,
}


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/integration/agent" in str(item.fspath):
            item.add_marker(pytest.mark.live)


@pytest.fixture
def live():
    if os.environ.get("WEATHER_SIGNAL_LIVE") != "1":
        pytest.skip("live agent tests spend Claude turns; set WEATHER_SIGNAL_LIVE=1")
    if os.environ.get("CREW_DATABRICKS_SERVICE", TEST_SERVICE) != TEST_SERVICE:
        pytest.fail("tests use the claude_subscription service, never the agent's own")
