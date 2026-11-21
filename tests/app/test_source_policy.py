"""The snapshot source: a document failing its sha256 is not served; the last good copy is kept."""

from __future__ import annotations

import json


def test_a_tampered_snapshot_is_refused_at_first_read(client):
    c, main, path = client
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["payload"]["outlook"][0]["shown_action"] = "buy"
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert c.get("/api/outlook").status_code == 503


def test_the_last_good_copy_is_kept(client):
    c, main, path = client
    assert c.get("/api/outlook").status_code == 200
    path.write_text("{not json", encoding="utf-8")
    main.source._checked = 0.0
    assert c.get("/api/outlook").status_code == 200


def test_auth_is_required_outside_local(client):
    c, main, _ = client
    main.REQUIRE_USER = True
    assert c.get("/api/outlook").status_code == 401
    assert c.get("/healthz").status_code == 200
    ok = c.get("/api/outlook", headers={"x-forwarded-email": "viewer@example.com"})
    assert ok.status_code == 200
