"""The CLI child sees three tools, no settings, no connectors, and a cwd under the agent's cache."""

import anyio
import pytest

pytest.importorskip("claude_agent_sdk")

from tests.unit.agent.test_submit_tool import AS_OF
from weather_signal.agent.options import MAX_TURNS, agent_cwd, options, prompt_text
from weather_signal.agent.tools import ALLOWED, Session, server


@pytest.fixture
def opts(tmp_path):
    s = Session(inputs={"2026-11-18": {"signal_action": "sell"}}, prices=[], as_of=AS_OF)
    return options(s, version="0.1.0", cwd=agent_cwd(tmp_path / "cache"))


def test_isolation_fields(opts, tmp_path):
    assert opts.max_turns == MAX_TURNS == 40
    assert opts.tools == []
    assert opts.setting_sources == []
    assert opts.strict_mcp_config is True
    assert opts.permission_mode == "dontAsk"
    assert sorted(opts.allowed_tools) == sorted(ALLOWED)
    assert len(ALLOWED) == 3
    assert opts.max_budget_usd and opts.max_budget_usd > 0
    assert opts.client_app == "weather-signal/0.1.0"
    assert str(opts.cwd).startswith(str(tmp_path / "cache"))
    assert list(opts.mcp_servers) == ["ws"]


def test_hook_blocks_on_failure(opts):
    (matcher,) = opts.hooks["PreToolUse"]
    assert matcher.matcher is None
    assert matcher.on_failure == "block"
    assert len(matcher.hooks) == 1


async def _listed():
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    srv = server(Session(inputs={}, prices=[], as_of=AS_OF))["instance"]
    async with create_client_server_memory_streams() as (cs, ss), anyio.create_task_group() as tg:
        tg.start_soon(lambda: srv.run(ss[0], ss[1], srv.create_initialization_options()))
        async with ClientSession(cs[0], cs[1]) as client:
            await client.initialize()
            tools = (await client.list_tools()).tools
        tg.cancel_scope.cancel()
    return {t.name: t.annotations for t in tools}


def test_read_tools_are_read_only():
    tools = anyio.run(_listed)
    assert tools["get_inputs"].read_only_hint is True
    assert tools["get_prices"].read_only_hint is True
    assert (
        tools["submit_recommendation"] is None or not tools["submit_recommendation"].read_only_hint
    )


def test_prompt_requires_a_breach_to_be_downgraded():
    """The prompt says what `_facts` enforces."""
    text = " ".join(prompt_text().split())
    assert "must set `downgrade` to true" in text
