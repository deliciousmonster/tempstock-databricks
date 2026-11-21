"""`/api/intraday`: the snapshot's bars and session calls, and empty, not an error, before any."""

from __future__ import annotations

import hashlib
import json

from tests.app.conftest import document


def _write(path, intraday):
    doc = document()
    doc.pop("sha256")
    if intraday is not None:
        doc["payload"]["intraday"] = intraday
    enc = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    doc["sha256"] = hashlib.sha256(enc).hexdigest()
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_the_route_serves_the_snapshots_bars_and_calls(client):
    c, _, path = client
    bars = [{"ticker": "WMT", "bar_start": "2026-11-18T14:30:00Z", "close": 100.0, "final": True}]
    calls = [
        {"as_of": "2026-11-18T15:30:00Z", "target_date": "2026-11-18", "shown_action": "buy",
         "signal_action": "buy", "last_close": 99.0, "est_price": None, "est_reason": "no_champion"}
    ]  # fmt: skip
    _write(path, {"bars": bars, "calls": calls})
    r = c.get("/api/intraday", headers={"x-forwarded-email": "a@b"})
    assert r.status_code == 200
    body = r.json()
    assert body["data"] == {"bars": bars, "calls": calls} and body["source"] == "snapshot"


def test_a_snapshot_from_before_intraday_answers_empty(client):
    c, _, _ = client
    r = c.get("/api/intraday", headers={"x-forwarded-email": "a@b"})
    assert r.status_code == 200 and r.json()["data"] == {"bars": [], "calls": []}


def test_the_route_is_get_only(client):
    c, _, _ = client
    assert c.post("/api/intraday").status_code == 405
