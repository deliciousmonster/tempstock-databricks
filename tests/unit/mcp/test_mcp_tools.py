"""The local MCP server: exactly five tools, every call row carries `integrity`, values only as parameters."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from weather_signal.config import Names
from weather_signal.serving import mcp_server

STATUS = {
    "open_incident": False,
    "post_verified_as_of": "2026-11-18T10:30:00Z",
    "guarded_tables": 2,
    "read_at": "2026-11-18T11:15:00Z",
}


def call(as_of: str) -> dict:
    return {
        "as_of": as_of,
        "target_date": "2026-11-19",
        "horizon_days": 1,
        "signal_action": "buy",
        "shown_action": "hold",
        "audit_problem": None,
        "written_at": as_of[:14] + "41:00Z",
    }


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def __call__(self, sql, params):
        self.sent.append((sql, params))
        if "fn_ui_status" in sql:
            return [dict(STATUS)]
        if "fn_ui_outlook" in sql or "fn_ui_calls" in sql:
            return [call("2026-11-18T09:30:00Z"), call("2026-11-18T10:30:00Z")]
        return []


@pytest.fixture
def tools():
    rec = Recorder()
    return mcp_server.Tools(Names("workspace", "dev_"), rec, lambda: "2026-11-18T11:20:00Z"), rec


def test_the_server_lists_exactly_five_tools(tools):
    t, _ = tools
    listed = asyncio.run(mcp_server.server(t).list_tools())
    assert sorted(x.name for x in listed) == sorted(mcp_server.TOOLS)


def test_every_call_row_carries_integrity_and_post_verified_equal_to_as_of_is_awaiting(tools):
    t, _ = tools
    rows = t.calls("2026-11-19")["rows"]
    assert [(r["integrity"], r["integrity_label"]) for r in rows] == [
        ("verified", "verified"),
        ("awaiting", "unknown: awaiting guard check"),
    ]
    assert all("integrity" in r for r in t.outlook()["rows"])


def test_values_reach_the_warehouse_only_as_parameters(tools):
    t, rec = tools
    with pytest.raises(ValueError):
        t.calls("2026-11-19') OR 1=1 --")
    with pytest.raises(ValueError):
        t.runs(10_000)
    t.calls("2026-11-19")
    t.runs(6)
    for sql, _ in rec.sent:
        assert sql.startswith("SELECT * FROM workspace.dev_app.fn_ui_") and "2026" not in sql
    assert ("2026-11-19", "DATE") in [v for _, p in rec.sent for v in p.values()]


def test_a_tool_answers_through_the_protocol(tools):
    t, _ = tools
    res = asyncio.run(mcp_server.server(t).call_tool("outlook", {}))
    assert json.loads(res.content[0].text)["rows"][1]["integrity"] == "awaiting"


@pytest.mark.parametrize(
    "day",
    [
        "２０２６-１１-１９",
        "٢٠٢٦-١١-١٩",
        "2026-13-45",
        "2026-02-30",
        " 2026-11-19",
        "20261119",
        20261119,
    ],
)
def test_a_date_that_is_not_an_ascii_calendar_date_never_reaches_the_warehouse(tools, day):
    """Fullwidth and Arabic-Indic digits and 2026-13-45 passed `\\d`."""
    t, rec = tools
    with pytest.raises(ValueError):
        t.calls(day)
    assert rec.sent == []


@pytest.mark.parametrize("hours", [0, 169, 1.5, True, "6"])
def test_hours_is_a_plain_int_in_range(tools, hours):
    t, rec = tools
    with pytest.raises(ValueError):
        t.runs(hours)
    assert rec.sent == []


def test_the_profile_selects_the_credentials_and_none_is_assumed(monkeypatch):
    """`--identity` was a label. Bootstrap: no profile or warehouse is pinned."""
    seen = {}
    monkeypatch.setattr(
        mcp_server,
        "warehouse_query",
        lambda profile, wh: seen.update(profile=profile) or (lambda s, p: []),
    )
    monkeypatch.setattr(
        mcp_server,
        "server",
        lambda tools: type("S", (), {"run": lambda self, t: seen.update(run=t)})(),
    )
    wh = ["--warehouse-id", "wh"]
    assert mcp_server.main([*wh, "--profile", "weather-signal"]) == 0
    assert seen == {"profile": "weather-signal", "run": "stdio"}
    mcp_server.main([*wh, "--profile", "weather-signal-reader"])
    assert seen["profile"] == "weather-signal-reader"
    with pytest.raises(SystemExit):
        mcp_server.main([*wh, "--identity", "reader"])
    with pytest.raises(SystemExit):
        mcp_server.main([*wh, "--schema-prefix", "dev_; DROP"])
    with pytest.raises(SystemExit):
        mcp_server.main([])
    with pytest.raises(SystemExit):
        mcp_server.main(wh)


def test_without_the_mcp_extra_the_server_names_it(monkeypatch, tools):
    monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", None)
    t, _ = tools
    with pytest.raises(SystemExit, match=r"weather-signal\[mcp\]"):
        mcp_server.server(t)
