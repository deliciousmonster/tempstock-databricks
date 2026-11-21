"""One Haiku turn through crewkit on `databricks-gateway`: three tools and `ws` only, no transcript written.

Spends 1 gateway turn.
"""

import json
import os
import re
import time
from pathlib import Path

import anyio
import pytest

from tests.integration.agent.conftest import GATEWAY_ENV
from tests.unit.agent.test_submit_tool import AS_OF
from weather_signal.agent.options import agent_cwd, options
from weather_signal.agent.tools import ALLOWED, Session

REPO = Path(__file__).resolve().parents[3]


def slugs(p: Path) -> set[str]:
    """The CLI's project slug for `p`, for the path as given and as resolved (/var vs /private/var)."""
    return {re.sub(r"[^A-Za-z0-9]", "-", str(q)) for q in (p, p.resolve())}


def newer(d: Path, t0: float) -> list[Path]:
    return (
        [p for p in d.rglob("*") if p.is_file() and p.stat().st_mtime >= t0] if d.exists() else []
    )


def test_isolated_turn(live, tmp_path, monkeypatch):
    for k, v in GATEWAY_ENV.items():
        monkeypatch.setenv(k, v)
    cwd = agent_cwd(tmp_path / "cache")
    projects = Path.home() / ".claude" / "projects"
    watched = [projects / x for x in slugs(REPO) | slugs(cwd)]
    s = Session(
        inputs={"2026-11-18": {"signal_action": "sell", "score": -0.4}}, prices=[], as_of=AS_OF
    )
    opts = options(s, version="test", cwd=cwd)
    init: dict = {}
    t0 = time.time()

    async def turn():
        from claude_agent_sdk import SystemMessage
        from crewkit.agent_sdk import connect

        async with connect(opts, timeout_s=30) as (client, verdict):
            assert verdict.route == "databricks-gateway"
            await client.query(
                "Call get_inputs for 2026-11-18, then submit it with downgrade false."
            )
            with anyio.fail_after(150):
                async for m in client.receive_response():
                    if isinstance(m, SystemMessage) and m.subtype == "init":
                        init.update(m.data)

    anyio.run(turn)
    if out := os.environ.get("ISOLATION_INIT_OUT"):
        keep = ("tools", "mcp_servers", "memory_paths", "cwd", "model", "apiKeySource")
        Path(out).write_text(json.dumps({k: init.get(k) for k in keep}, indent=2))
    assert sorted(init["tools"]) == sorted(ALLOWED)
    assert [m["name"] for m in init.get("mcp_servers", [])] == ["ws"]
    mem = init.get("memory_paths") or {}
    paths = [str(v) for v in (mem.values() if isinstance(mem, dict) else mem)]
    assert all(any(x in v for x in slugs(cwd)) for v in paths), paths
    for d in watched:
        assert newer(d, t0) == [], f"the CLI wrote under {d}"
    if not s.proposals:
        pytest.fail("the turn submitted nothing")
