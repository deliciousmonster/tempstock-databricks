"""U2 at serve time: every call carries its state now and at the snapshot's read."""

from __future__ import annotations

from datetime import UTC, datetime


def test_outlook_rows_carry_integrity_computed_now(client):
    c, main, _ = client
    main.now_iso = lambda: "2026-11-18T11:20:00Z"
    rows = c.get("/api/outlook").json()["data"]
    assert len(rows) == 7 and {r["integrity"] for r in rows} == {"awaiting"}
    prev = c.get("/api/calls/2026-11-18T09:30:00Z/2026-11-19").json()["data"]
    assert [r["integrity"] for r in prev] == ["verified"]


def test_an_old_snapshot_is_never_verified(client):
    c, main, _ = client
    main.now_iso = lambda: datetime(2026, 11, 19, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    prev = c.get("/api/calls/2026-11-18T09:30:00Z/2026-11-19").json()["data"][0]
    assert prev["integrity"] == "stale" and prev["integrity_at_read"] == "verified"
    assert prev["integrity_label"].startswith("unknown: integrity as of")
