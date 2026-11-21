"""PreToolUse gate for `execute_sql`: an allowlist, one plain read of the six `v_ui_*` views and five `fn_ui_*`.

Names must be exact, in the configured catalog and app schema (`WS_UI_CATALOG`, `WS_UI_SCHEMA`;
by default `workspace` and `app` or `dev_app`). Weaker than the reader's grants: it parses the
model's text. It never allows on its own; an allowed statement still meets Claude Code's prompt.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# Equal to weather_signal.serving.views.VIEWS and the MCP server's functions (test_sql_gate checks).
VIEWS = frozenset(
    {"v_ui_calls", "v_ui_outlook", "v_ui_status", "v_ui_index", "v_ui_agent_runs", "v_ui_sources"}
)
FUNCTIONS = frozenset(
    {"fn_ui_status", "fn_ui_outlook", "fn_ui_calls", "fn_ui_runs", "fn_ui_sources"}
)


def _place() -> tuple[str, frozenset[str]]:
    schema = os.environ.get("WS_UI_SCHEMA", "").lower()
    return os.environ.get("WS_UI_CATALOG", "workspace").lower(), frozenset(
        {schema} if schema else {"app", "dev_app"}
    )


def verdict(sql: str) -> str | None:
    """None when the statement may run; otherwise why not. Anything unparsed or unlisted denies."""
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return "sqlglot is not installed, so nothing can be checked"
    try:
        trees = [t for t in sqlglot.parse(sql, read="databricks") if t is not None]
    except Exception as err:  # noqa: BLE001 - a parser gap must deny, never allow
        return f"unparsable as Databricks SQL ({type(err).__name__})"
    if len(trees) != 1:
        return "only one statement may run"
    tree = trees[0]
    while isinstance(tree, exp.Subquery):
        tree = tree.this
    if not isinstance(tree, exp.Select | exp.Union | exp.Intersect | exp.Except):
        return "only a SELECT, WITH ... SELECT or set operation may run"
    return next((why for node in tree.walk() if (why := _node(node))), None)


def _cte_in_scope(node: Any, name: str) -> bool:
    """True when a WITH enclosing `node` defines `name` before the CTE `node` sits in (build r3 S1)."""
    from sqlglot import exp

    child, cte, parent = node, None, node.parent
    while parent is not None:
        if isinstance(parent, exp.CTE):
            cte = parent
        elif isinstance(parent, exp.With):
            ctes = list(parent.expressions)
            upto = next((i for i, c in enumerate(ctes) if c is cte), len(ctes))
            if name in {c.alias_or_name.lower() for c in ctes[:upto]}:
                return True
            cte = None
        elif (w := parent.args.get("with_")) is not None and child is not w:
            if name in {c.alias_or_name.lower() for c in w.expressions}:
                return True
        child, parent = parent, parent.parent
    return False


def _names(e: Any) -> list[str] | None:
    """The dotted names of a qualifier, or None for anything but identifiers."""
    from sqlglot import exp

    if isinstance(e, exp.Identifier):
        return [e.name]
    if isinstance(e, exp.Dot) and isinstance(e.expression, exp.Identifier):
        head = _names(e.this)
        return None if head is None else [*head, e.expression.name]
    return None


def _where(catalog: str, db: str) -> str | None:
    """Why a catalog and schema are not the configured ones, or None."""
    want, schemas = _place()
    if catalog and catalog.lower() != want:
        return f"catalog {catalog} is not {want}"
    if db and db.lower() not in schemas:
        return f"schema {db} is not {' or '.join(sorted(schemas))}"
    return None


def _node(node: Any) -> str | None:
    """Why one node of a read may not run, or None."""
    from sqlglot import exp

    denied = (exp.Into, exp.QueryTransform, exp.Command, exp.Insert, exp.Update, exp.Delete)
    denied += (exp.Merge, exp.Create, exp.Drop, exp.Alter, exp.TruncateTable)
    if isinstance(node, denied):
        return f"{type(node).__name__} is not a read"
    if isinstance(node, exp.Anonymous) and node.name.lower() not in FUNCTIONS:
        return f"function {node.name} is not one of the five fn_ui_*"
    if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Anonymous):
        qual = _names(node.this)  # a scalar call's qualifier, checked as a table's (build r4 S2)
        if qual is None or len(qual) > 2:
            return f"function qualifier {node.this.sql()} is not catalog.schema"
        return _where(*([""] * (2 - len(qual)) + qual))
    if not isinstance(node, exp.Table):
        return None
    if why := _where(node.catalog, node.db):
        return why
    name = node.name.lower()
    if isinstance(node.this, exp.Identifier) and name not in VIEWS:
        if node.db or not _cte_in_scope(node, name):
            return f"{node.name} is not one of the six v_ui_* views or a CTE in scope"
    return None


def decide(event: dict[str, Any]) -> dict[str, Any] | None:
    sql = (event.get("tool_input") or {}).get("query")
    why = verdict(sql) if isinstance(sql, str) else "no query string"
    if why is None:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"sql_gate: {why}",
        }
    }


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    out = decide(event)
    if out is not None:
        json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
