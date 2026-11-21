"""U1 at the API: GET only, and a request cannot become arbitrary SQL."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize("path", ["/api/outlook", "/api/calls", "/healthz", "/api/anything"])
def test_every_other_method_is_405(client, method, path):
    c, _, _ = client
    assert getattr(c, method)(path).status_code == 405


def test_every_route_is_get_only(client):
    _, main, _ = client
    methods = {m for r in main.app.routes for m in (getattr(r, "methods", None) or set())}
    assert methods <= {"GET", "HEAD"}


def test_live_queries_are_constant_selects_over_ui_views(client):
    from ws_ui.queries import SQL

    for key, sql in SQL.items():
        assert sql.lstrip().upper().startswith("SELECT"), key
        assert ";" not in sql and "{" not in sql, key
        assert all(t.startswith("v_ui_") for t in __import__("re").findall(r"FROM\s+(\w+)", sql)), (
            key
        )


def test_bad_parameters_are_refused_before_any_query(client):
    c, _, _ = client
    assert c.get("/api/calls?horizon=7").status_code == 422
    assert c.get("/api/calls/2026-11-18T10:31:00Z/2026-11-18").status_code == 422
    assert c.get("/api/calls/x';DROP/2026-11-18").status_code == 422
    assert c.get("/api/calls?from=2024-01-01&to=2026-01-01").status_code == 422
