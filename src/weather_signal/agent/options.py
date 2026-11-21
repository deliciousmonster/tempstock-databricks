"""The ClaudeAgentOptions the worker builds; crewkit refuses the isolation fields if they're wrong."""

from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path
from typing import Any

from weather_signal.agent.tools import ALLOWED, SERVER, Session, deny_outside_three, server
from weather_signal.config import PRIMARY

MODEL = "haiku"
MAX_TURNS = 40
MAX_BUDGET_USD = 0.05
CACHE = Path.home() / "Library" / "Caches" / "weather-signal"


def agent_cwd(cache: Path = CACHE) -> Path:
    path = cache / "agent-cwd"
    path.mkdir(parents=True, exist_ok=True)
    return path


def prompt_text() -> str:
    text = resources.files("weather_signal.agent").joinpath("prompt.md").read_text(encoding="utf-8")
    return text.replace("{ticker}", PRIMARY)


def prompt_sha() -> str:
    return hashlib.sha256(prompt_text().encode()).hexdigest()


def options(session: Session, *, version: str, cwd: Path, cli_path: str | None = None) -> Any:
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    async def hook(hook_input: Any, tool_use_id: str | None, ctx: Any) -> Any:
        return await deny_outside_three(hook_input, tool_use_id, ctx, session=session)

    return ClaudeAgentOptions(
        model=MODEL,
        system_prompt=prompt_text(),
        tools=[],
        allowed_tools=list(ALLOWED),
        mcp_servers={SERVER: server(session)},
        strict_mcp_config=True,
        setting_sources=[],
        permission_mode="dontAsk",
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[hook], on_failure="block")]},
        max_turns=MAX_TURNS,
        max_budget_usd=MAX_BUDGET_USD,
        client_app=f"weather-signal/{version}",
        cwd=str(cwd),
        cli_path=cli_path,
    )
