"""The local MCP server on dev, as the owner (the reader is owner-gated): five tools; calls carry integrity."""

import asyncio
import json

from tests.integration.conftest import PROFILE, warehouse_id
from weather_signal.serving import mcp_server


def test_five_tools_and_calls_carry_integrity(n):
    tools = mcp_server.Tools(n, mcp_server.warehouse_query(PROFILE, warehouse_id()))
    s = mcp_server.server(tools)
    assert sorted(t.name for t in asyncio.run(s.list_tools())) == sorted(mcp_server.TOOLS)
    out = json.loads(asyncio.run(s.call_tool("outlook", {})).content[0].text)
    assert out["rows"] and all(r["integrity"] for r in out["rows"])
    day = out["rows"][0]["target_date"]
    calls = json.loads(asyncio.run(s.call_tool("calls", {"target_date": day})).content[0].text)
    assert calls["rows"] and all("integrity" in r for r in calls["rows"])
