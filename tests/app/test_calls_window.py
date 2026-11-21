"""A horizon or range the snapshot lacks is read live, never an empty 200."""

from __future__ import annotations


def test_a_horizon_outside_the_snapshot_is_not_an_empty_200(client):
    c, _, _ = client
    r = c.get("/api/calls?horizon=3")
    assert r.status_code == 503 and "live" in r.json()["detail"]


def test_a_range_before_the_window_goes_live(client):
    c, main, _ = client
    seen = {}

    def live(key, params):
        seen.update(key=key, **params)
        return [{"as_of": "2025-01-02T10:30:00Z", "target_date": "2025-01-02", "horizon_days": 3}]

    main.source.live = live
    body = c.get("/api/calls?horizon=3&from=2024-12-01&to=2025-01-10").json()
    assert body["source"] == "live" and seen == {
        "key": "calls",
        "horizon": "3",
        "start": "2024-12-01",
        "end": "2025-01-10",
    }
    assert [r["target_date"] for r in body["data"]] == ["2025-01-02"]


def test_horizon_0_in_the_window_stays_on_the_snapshot(client):
    c, _, _ = client
    body = c.get("/api/calls?horizon=0&from=2026-11-01&to=2026-11-30").json()
    assert body["source"] == "snapshot" and body["data"]
