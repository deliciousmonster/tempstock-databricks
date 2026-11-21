"""Owner, 2026-11-18: /api/backtest serves the newest run and the full-range index, live and cached."""

from __future__ import annotations

from types import SimpleNamespace


def test_backtest_is_read_live_once_and_kept(client, monkeypatch):
    c, main, _ = client
    reads: list[str] = []

    def live(key, params):
        reads.append(key)
        return [{"key": key}]

    monkeypatch.setattr(main.source, "live", live)
    main._backtest.clear()
    first = c.get("/api/backtest").json()
    second = c.get("/api/backtest").json()
    assert first["source"] == "live"
    assert first["data"] == {
        "backtest": [{"key": "backtest"}],
        "index": [{"key": "index_full"}],
        "measured": None,
    }
    assert second["data"] == first["data"] and reads == ["backtest", "index_full"]


def test_backtest_is_refetched_after_its_ttl(client, monkeypatch):
    c, main, _ = client
    reads: list[str] = []
    monkeypatch.setattr(main.source, "live", lambda key, params: reads.append(key) or [])
    main._backtest.clear()
    c.get("/api/backtest")
    main._backtest["at"] -= main.BACKTEST_TTL_S + 1
    c.get("/api/backtest")
    assert reads == ["backtest", "index_full"] * 2


def test_no_live_read_is_a_503_not_an_empty_chart(client):
    c, main, _ = client
    main._backtest.clear()
    r = c.get("/api/backtest")
    assert r.status_code == 503 and "live reads are off" in r.json()["detail"]


def test_a_result_past_one_chunk_is_read_to_the_end(client):
    from ws_ui.source import Source

    chunks = {1: SimpleNamespace(data_array=[["b"]], next_chunk_index=2)}
    chunks[2] = SimpleNamespace(data_array=[["c"]], next_chunk_index=None)
    src = Source()
    src._client = SimpleNamespace(
        statement_execution=SimpleNamespace(get_statement_result_chunk_n=lambda sid, n: chunks[n])
    )
    first = SimpleNamespace(
        statement_id="s", result=SimpleNamespace(data_array=[["a"]], next_chunk_index=1)
    )
    assert src._all_chunks(first) == [["a"], ["b"], ["c"]]
