"""App tests. FastAPI comes from the `app` dependency group; a missing one is an error, never a skip."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"
sys.path.insert(0, str(APP))

STATUS = {
    "open_incident": False,
    "post_verified_as_of": "2026-11-18T10:30:00Z",
    "guarded_tables": 2,
    "read_at": "2026-11-18T11:15:00Z",
    "latest_signal_as_of": "2026-11-18T10:30:00Z",
}


def call(as_of: str, target: str, h: int, **kw) -> dict:
    row = {
        "as_of": as_of,
        "target_date": target,
        "horizon_days": h,
        "signal_action": "buy",
        "shown_action": "hold",
        "source": "agent",
        "audit_problem": None,
        "written_at": as_of[:14] + "41:00Z",
        "trace_id": None,
        "rationale": "<script>x</script>",
    }
    row.update(kw)
    return row


def document() -> dict:
    prev = [call("2026-11-18T09:30:00Z", f"2026-11-{18 + h}", h) for h in range(7)]
    new = [call("2026-11-18T10:30:00Z", f"2026-11-{18 + h}", h) for h in range(7)]
    payload = {
        "status": STATUS,
        "sources": [],
        "outlook": new,
        "evidence": prev + new,
        "history": prev + new,
        "index": [{"date": "2026-11-17", "close": 200.0, "anomaly_c": None}],
        "runs": [],
        "traces": {},
    }
    doc = {
        "schema": 3,
        "built_at": "2026-11-18T11:15:00Z",
        "read_at": STATUS["read_at"],
        "payload": payload,
    }
    enc = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    doc["sha256"] = hashlib.sha256(enc).hexdigest()
    return doc


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(document()), encoding="utf-8")
    monkeypatch.setenv("WS_UI_LOCAL", "1")
    monkeypatch.setenv("WS_UI_SNAPSHOT", str(path))
    for mod in [m for m in sys.modules if m.startswith("ws_ui")]:
        del sys.modules[mod]
    from fastapi.testclient import TestClient
    from ws_ui import main

    return TestClient(main.app), main, path
