"""`weather-signal-mcp`: the beat's local MCP server, five read tools over `fn_ui_*`.

Every call row carries `integrity`, computed by the App's rule when the tool runs, so Claude is
handed the state and never derives it from guard inputs.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any

from weather_signal.config import Names
from weather_signal.serving.app_bridge import module

Query = Callable[[str, dict[str, tuple[str, str]]], list[dict[str, Any]]]
GUARD = ("open_incident", "post_verified_as_of", "guarded_tables", "read_at")
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
IDENT = re.compile(r"[a-z_][a-z0-9_]*")
TOOLS = ("outlook", "calls", "runs", "sources", "status")
NEEDS_EXTRA = "needs the mcp extra: pip install 'weather-signal[mcp]'"


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Tools:
    """The tools' logic over `query(sql, {name: (value, type)})`, so it is tested without a warehouse."""

    def __init__(self, n: Names, query: Query, now: Callable[[], str] = now_iso) -> None:
        self.app, self.query, self.now = n.schema("app"), query, now
        self.integrity = module("integrity")

    def _status(self) -> dict[str, Any]:
        rows = self.query(f"SELECT * FROM {self.app}.fn_ui_status()", {})
        return rows[0] if rows else {}

    def _with_integrity(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        status, t = self._status(), self.now()
        for r in rows:
            key = self.integrity.state({**r, **{k: status.get(k) for k in GUARD}}, t)
            r["integrity"] = key
            r["integrity_label"] = self.integrity.label(key, status.get("read_at"))
        return {"computed_at": t, "read_at": status.get("read_at"), "rows": rows}

    def outlook(self) -> dict[str, Any]:
        return self._with_integrity(self.query(f"SELECT * FROM {self.app}.fn_ui_outlook()", {}))

    def calls(self, target_date: str) -> dict[str, Any]:
        if not (isinstance(target_date, str) and DAY.fullmatch(target_date)):
            raise ValueError("target_date is YYYY-MM-DD in ASCII digits")
        date.fromisoformat(target_date)  # a real calendar date, or ValueError
        sql = f"SELECT * FROM {self.app}.fn_ui_calls(:target_date)"
        return self._with_integrity(self.query(sql, {"target_date": (target_date, "DATE")}))

    def runs(self, hours: int = 24) -> dict[str, Any]:
        if type(hours) is not int or not 1 <= hours <= 168:
            raise ValueError("hours is 1 to 168")
        return {
            "rows": self.query(
                f"SELECT * FROM {self.app}.fn_ui_runs(:hours)", {"hours": (str(hours), "INT")}
            )
        }

    def sources(self) -> dict[str, Any]:
        return {"rows": self.query(f"SELECT * FROM {self.app}.fn_ui_sources()", {})}

    def status(self) -> dict[str, Any]:
        return {"rows": [self._status()]}


def warehouse_query(profile: str, warehouse_id: str) -> Query:
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.sql import (
        ExecuteStatementRequestOnWaitTimeout,
        StatementParameterListItem,
        StatementState,
    )

    w, typed = WorkspaceClient(profile=profile), module("source").typed

    def query(sql: str, params: dict[str, tuple[str, str]]) -> list[dict[str, Any]]:
        r = w.statement_execution.execute_statement(
            statement=sql,
            warehouse_id=warehouse_id,
            parameters=[
                StatementParameterListItem(name=k, value=v, type=t) for k, (v, t) in params.items()
            ],
            wait_timeout="30s",
            on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CANCEL,
        )
        if r.status is None or r.status.state != StatementState.SUCCEEDED or r.manifest is None:
            raise RuntimeError(f"statement {getattr(r.status, 'state', None)}")
        columns = (r.manifest.schema.columns if r.manifest.schema else None) or []
        cols = [(c.name, str(getattr(c.type_name, "value", c.type_name))) for c in columns]
        return list(typed(cols, (r.result.data_array if r.result else None) or []))

    return query


def server(tools: Tools) -> Any:
    try:
        from mcp.server.mcpserver import MCPServer
    except ModuleNotFoundError as err:
        raise SystemExit(f"weather-signal-mcp: {NEEDS_EXTRA}") from err

    s = MCPServer(
        "weather-signal",
        instructions="A call's integrity is the `integrity` field; never derive it from guard inputs. "
        "The newest outlook is `awaiting guard check` by design. Personal demo, not investment advice.",
    )

    @s.tool()
    def outlook() -> str:
        """The latest outlook, today and the next six days, each call with its integrity now."""
        return json.dumps(tools.outlook())

    @s.tool()
    def calls(target_date: str) -> str:
        """Every call for one date (YYYY-MM-DD), each with its integrity now."""
        return json.dumps(tools.calls(target_date))

    @s.tool()
    def runs(hours: int = 24) -> str:
        """Agent runs started in the last `hours` (1 to 168)."""
        return json.dumps(tools.runs(hours))

    @s.tool()
    def sources() -> str:
        """Each source's last landing, freshness, the pipeline update and the expectation counts."""
        return json.dumps(tools.sources())

    @s.tool()
    def status() -> str:
        """The guard inputs and when they were read."""
        return json.dumps(tools.status())

    return s


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="weather-signal-mcp",
        description=f"The local MCP server over the App's read functions; {NEEDS_EXTRA}.",
    )
    ap.add_argument(
        "--profile",
        required=True,
        help="the ~/.databrickscfg profile whose credentials every tool uses: the owner's, or "
        "the reader's (`weather-signal-reader`) once the owner creates it",
    )
    ap.add_argument("--catalog", default="workspace")
    ap.add_argument("--schema-prefix", default="")
    ap.add_argument("--warehouse-id", required=True, help="`databricks warehouses list`")
    a = ap.parse_args(argv)
    if not IDENT.fullmatch(a.catalog) or not (
        a.schema_prefix == "" or IDENT.fullmatch(a.schema_prefix)
    ):
        ap.error("--catalog and --schema-prefix are lower-case identifiers")
    tools = Tools(Names(a.catalog, a.schema_prefix), warehouse_query(a.profile, a.warehouse_id))
    server(tools).run("stdio")
    return 0
