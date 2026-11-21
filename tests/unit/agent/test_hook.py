"""The PreToolUse hook denies every tool but the three, by exact name."""

import anyio
import pytest

from tests.unit.agent.test_submit_tool import AS_OF
from weather_signal.agent.tools import ALLOWED, Session, deny_outside_three


def decide(name):
    s = Session(inputs={}, prices=[], as_of=AS_OF)
    out = anyio.run(
        lambda: deny_outside_three(
            {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": {}},
            None,
            None,
            session=s,
        )
    )
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision", "allow"), s.denials


@pytest.mark.parametrize(
    "name", ["Bash", "Skill", "mcp__other__x", "mcp__ws__other", "mcp__ws", "Read"]
)
def test_denied(name):
    assert decide(name) == ("deny", 1)


@pytest.mark.parametrize("name", ALLOWED)
def test_allowed(name):
    assert decide(name) == ("allow", 0)
