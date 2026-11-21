"""The snapshot job's document is one the App accepts (its own sha256 check) and the page can render."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from tests.contract.snapshot_contract import CALL, INTRADAY, problems
from tests.unit.ui.test_integrity import ROOT
from weather_signal.serving import snapshot

NOW = datetime(2026, 11, 18, 12, 15)


def app_verify():
    import sys

    sys.path.insert(0, str(ROOT / "app"))
    from ws_ui import source

    return source._verify


SOURCES = [
    {
        "kind": "source",
        "layer": "raw",
        "name": "alphavantage",
        "at": NOW,
        "state": None,
        "n": None,
        "n_bad": None,
    },
    {
        "kind": "fresh",
        "layer": "signals",
        "name": "temp_index_daily",
        "at": NOW,
        "state": None,
        "n": None,
        "n_bad": None,
    },
    {
        "kind": "update",
        "layer": "pipeline",
        "name": "update",
        "at": NOW,
        "state": "COMPLETED",
        "n": None,
        "n_bad": None,
    },
    {
        "kind": "expectations",
        "layer": "daily",
        "name": "prices_daily",
        "at": NOW,
        "state": None,
        "n": 100,
        "n_bad": 2,
    },
]

INTRADAY_BARS = [
    {
        "ticker": "WMT",
        "bar_start": datetime(2026, 11, 18, 10 + h, 30),
        "session_date": date(2026, 11, 18),
        "close": 100.0 + h,
        "volume": 10,
        "final": h < 1,
        "fetched_at": datetime(2026, 11, 18, 12, 5),
    }
    for h in range(2)
]


def rows() -> dict:
    calls = []
    for h in range(2):
        a = datetime(2026, 11, 18, 10 + h, 30)
        for k in range(7):
            c = {key: None for key in CALL} | {"est_price": None, "est_reason": "hold"}
            c.update(
                as_of=a,
                target_date=a.date() + timedelta(days=k),
                horizon_days=k,
                signal_action="buy",
                written_at=a + timedelta(minutes=11),
                forecast_anomaly_c=1.5,
                trace_id=f"tr-{h}{k}",
            )
            calls.append(c)
    return {
        "v_ui_calls": calls,
        "v_ui_outlook": calls[7:],
        "v_ui_status": [
            {
                "open_incident": False,
                "post_verified_as_of": datetime(2026, 11, 18, 10, 30),
                "guarded_tables": 2,
                "read_at": NOW,
            }
        ],
        "v_ui_index": [{"date": date(2026, 11, 17), "close": 200.0, "anomaly_c": None}],
        "v_ui_agent_runs": [
            {
                "as_of": datetime(2026, 11, 18, 11, 30),
                "started_at": datetime(2026, 11, 18, 11, 40),
                "route": "gateway",
                "outcome": "ok",
                "duration_s": 31.0,
            }
        ],
        "v_ui_sources": SOURCES,
        "v_ui_intraday": INTRADAY_BARS,
    }


def test_a_built_snapshot_meets_the_contract_and_the_app_accepts_it():
    doc = json.loads(json.dumps(snapshot.build(rows(), {}, None, NOW)))
    assert problems(doc) == []
    assert app_verify()(doc)
    doc["payload"]["outlook"][0]["shown_action"] = "sell"
    assert not app_verify()(doc)


def test_every_field_the_page_reads_is_in_the_contract():
    """A call or source field app.js reads that the contract does not require is a gap."""
    import re

    from tests.contract.snapshot_contract import SECTIONS

    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    sources = js[js.index("function sourcesStrip") : js.index("function outlook")]
    read = set(re.findall(r"\br\.([a-z_][a-z0-9_]*)", sources))
    assert read <= SECTIONS["sources"], read - SECTIONS["sources"]
    page_only = {"trace_summary"}  # added by the route, not the snapshot
    calls = set(re.findall(r"\bc\.([a-z_][a-z0-9_]*)", js)) - page_only
    assert calls <= SECTIONS["outlook"], calls - SECTIONS["outlook"]


def test_a_built_snapshot_carries_every_source_kind():
    doc = json.loads(json.dumps(snapshot.build(rows(), {}, None, NOW)))
    kinds = {r["kind"] for r in doc["payload"]["sources"]}
    assert {"source", "fresh", "update", "expectations"} <= kinds and problems(doc) == []


def _section(js: str, start: str, end: str) -> str:
    return js[js.index(start) : js.index(end)]


def test_every_field_the_intraday_page_reads_is_in_the_contract():
    import re

    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    page = _section(js, "var MARK =", "function runs()")
    bars = set(re.findall(r"\bbar\.([a-z_][a-z0-9_]*)", page))
    calls = set(re.findall(r"\bk\.([a-z_][a-z0-9_]*)", page))
    assert bars and bars <= INTRADAY["bars"], bars - INTRADAY["bars"]
    assert {"est_price", "est_reason"} <= calls <= INTRADAY["calls"], calls - INTRADAY["calls"]


def test_a_built_snapshot_carries_intraday_bars_and_session_calls():
    doc = json.loads(json.dumps(snapshot.build(rows(), {}, None, NOW)))
    got = doc["payload"]["intraday"]
    assert [b["bar_start"] for b in got["bars"]] == ["2026-11-18T10:30:00Z", "2026-11-18T11:30:00Z"]
    assert {c["as_of"] for c in got["calls"]} == {"2026-11-18T10:30:00Z", "2026-11-18T11:30:00Z"}
    assert problems(doc) == []
    del got["calls"][0]["est_reason"]
    assert problems(doc) == ["intraday.calls[0] lacks ['est_reason']"]
