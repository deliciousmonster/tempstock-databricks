"""The PreToolUse gate: the plan's 15 cases, and the hook's JSON contract on stdin and stdout."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HOOK = ROOT / ".claude" / "hooks" / "sql_gate.py"
sys.path.insert(0, str(HOOK.parent))
import sql_gate  # noqa: E402

APP = "workspace.app"
CASES = [
    ("select 1 -- trailing comment", True),
    ("CREATE TABLE workspace.default.x (a INT)", False),
    ("INSERT INTO workspace.default.x VALUES (1)", False),
    ("SELECT 1; DROP TABLE workspace.default.x", False),
    ("/* SELECT */ DELETE FROM workspace.default.x", False),
    ("SELECT 1 /* ; */; INSERT INTO workspace.default.x VALUES (1)", False),
    ("CREATE TABLE workspace.default.y AS SELECT 1", False),
    ("MERGE INTO t USING s ON t.a = s.a WHEN MATCHED THEN DELETE", False),
    ("GRANT SELECT ON TABLE t TO `x`", False),
    ("CALL system.foo()", False),
    ("OPTIMIZE workspace.default.x", False),
    ("SET spark.x = 1", False),
    ("", False),
    # Reads with side effects, and the allowlist's edges.
    ("SELECT http_request('http://x')", False),
    ("SELECT reflect('java.lang.Runtime', 'getRuntime')", False),
    ("SELECT java_method('java.lang.Runtime', 'getRuntime')", False),
    ("SELECT * FROM read_files('s3://bucket/x')", False),
    ("SELECT ai_query('m', 'p')", False),
    ("SELECT TRANSFORM(a) USING 'cat' FROM workspace.app.v_ui_calls", False),
    ("SELECT 1 INTO t", False),
    ("SELECT * FROM workspace.signals.signal", False),
    ("SELECT * FROM workspace.dev_ops.v_ui_calls", False),
    ("WITH c AS (SELECT 1) SELECT * FROM workspace.signals.c", False),
    ("(SELECT * FROM workspace.dev_app.v_ui_status)", True),
    ("SELECT * FROM workspace.dev_app.fn_ui_calls(DATE'2026-11-19')", True),
    (
        "SELECT target_date, count(*) FROM v_ui_calls GROUP BY 1 UNION ALL SELECT target_date, 0 FROM v_ui_outlook",
        True,
    ),
    # CTE scope, and exact catalog, schema and names.
    ("WITH obs AS (SELECT * FROM obs) SELECT * FROM obs", False),
    ("SELECT * FROM (WITH t AS (SELECT 1) SELECT * FROM t) x, t", False),
    ("WITH a AS (SELECT * FROM b), b AS (SELECT * FROM v_ui_calls) SELECT * FROM a", False),
    ("SELECT * FROM other_cat.app.v_ui_calls", False),
    ("SELECT * FROM `other`.`signals`.`v_ui_calls`", False),
    ("SELECT * FROM evil_signals.v_ui_calls", False),
    ("SELECT * FROM workspace.app.v_ui_x_secret", False),
    ("SELECT * FROM workspace.dev_app.fn_ui_evil()", False),
    # A function's qualifier, as a table's.
    ("SELECT other.app.fn_ui_calls(1)", False),
    ("SELECT raw.fn_ui_calls()", False),
    ("SELECT * FROM v_ui_calls WHERE x = evil.fn_ui_outlook(1)", False),
    ("SELECT workspace.dev_app.fn_ui_status()", True),
    ("SELECT app.fn_ui_status()", True),
    (
        "WITH a AS (SELECT * FROM workspace.app.v_ui_calls), b AS (SELECT * FROM a) SELECT * FROM b",
        True,
    ),
    ("SELECT * FROM (WITH t AS (SELECT * FROM v_ui_calls) SELECT * FROM t) x", True),
]


# The App's own reads, from the app schema.
APP_READS = [
    (f"SELECT * FROM {APP}.fn_ui_status()", True),
    (f"WITH c AS (SELECT * FROM {APP}.v_ui_calls) SELECT * FROM c", True),
]


@pytest.mark.parametrize(("sql", "ok"), APP_READS + CASES)
def test_case(sql, ok):
    assert (sql_gate.verdict(sql) is None) is ok


def run(event) -> str:
    r = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout


def test_a_write_is_denied_with_a_reason():
    out = json.loads(
        run(
            {
                "tool_name": "mcp__databricks-sql__execute_sql",
                "tool_input": {"query": "DROP TABLE t"},
            }
        )
    )
    h = out["hookSpecificOutput"]
    assert h["permissionDecision"] == "deny" and h["permissionDecisionReason"].startswith(
        "sql_gate: "
    )


def test_a_read_is_left_to_claude_codes_own_prompt():
    assert run({"tool_input": {"query": "SELECT * FROM workspace.app.v_ui_status"}}) == ""


@pytest.mark.parametrize("event", [{}, {"tool_input": {"query": 1}}, {"tool_input": None}])
def test_a_malformed_event_is_denied(event):
    assert json.loads(run(event))["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_configured_place_is_exact(monkeypatch):
    monkeypatch.setenv("WS_UI_CATALOG", "main")
    monkeypatch.setenv("WS_UI_SCHEMA", "dev_app")
    assert sql_gate.verdict("SELECT * FROM main.dev_app.v_ui_calls") is None
    assert sql_gate.verdict("SELECT * FROM workspace.dev_app.v_ui_calls") is not None
    assert sql_gate.verdict("SELECT * FROM main.app.v_ui_calls") is not None


def test_the_allowed_names_are_the_views_and_the_mcp_functions():
    import re

    from weather_signal.serving import views

    mcp = (ROOT / "src/weather_signal/serving/mcp_server.py").read_text(encoding="utf-8")
    assert sql_gate.VIEWS == set(views.VIEWS)
    assert sql_gate.FUNCTIONS == set(re.findall(r"\.(fn_ui_\w+)\(", mcp))
