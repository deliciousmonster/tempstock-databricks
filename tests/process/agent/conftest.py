import pytest

pytest.importorskip("claude_agent_sdk", reason="needs the agent extra: uv sync --extra agent")

from tests.process.agent.harness import Gateway, World


@pytest.fixture
def gateway():
    gw = Gateway()
    yield gw
    gw.close()


@pytest.fixture
def world(tmp_path, gateway):
    return World(tmp_path, gateway)
