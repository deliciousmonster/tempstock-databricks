"""The snapshot builder: ISO `Z` timestamps, traces carried forward, and degrade instead of refuse."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from weather_signal.serving import snapshot

NOW = datetime(2026, 11, 18, 12, 15)


def call(as_of: datetime, target: date, h: int, **kw) -> dict:
    return {
        "as_of": as_of,
        "target_date": target,
        "horizon_days": h,
        "signal_action": "buy",
        "shown_action": kw.pop("shown_action", "hold"),
        "source": "agent",
        "audit_problem": None,
        "last_close": 200.0,
        "written_at": as_of + timedelta(minutes=11),
        "trace_id": kw.pop("trace_id", None),
        "rationale": kw.pop("rationale", "why"),
        "est_price": kw.pop("est_price", None),
        "est_reason": kw.pop("est_reason", "hold"),
        **kw,
    }


def rows(calls: list[dict]) -> dict:
    latest = max(c["as_of"] for c in calls)
    return {
        "v_ui_calls": calls,
        "v_ui_outlook": [c for c in calls if c["as_of"] == latest],
        "v_ui_status": [
            {
                "open_incident": False,
                "post_verified_as_of": latest,
                "guarded_tables": 2,
                "read_at": NOW,
            }
        ],
        "v_ui_index": [{"date": date(2026, 11, 17), "close": 200.0, "anomaly_c": None}],
        "v_ui_agent_runs": [],
        "v_ui_sources": [],
    }


def test_timestamps_end_in_z_and_the_document_verifies():
    a = datetime(2026, 11, 18, 11, 30)
    doc = snapshot.build(rows([call(a, date(2026, 11, 19), 1)]), {}, None, NOW)
    assert doc["read_at"] == "2026-11-18T12:15:00Z" and doc["built_at"].endswith("Z")
    assert doc["payload"]["outlook"][0]["written_at"] == "2026-11-18T11:41:00Z"
    assert snapshot.verify(doc)
    doc["payload"]["outlook"][0]["shown_action"] = "buy"
    assert not snapshot.verify(doc)


def test_summaries_are_carried_forward_and_only_new_ids_searched():
    a = datetime(2026, 11, 18, 11, 30)
    calls = [
        call(a, date(2026, 11, 19), 1, trace_id="tr-old"),
        call(a, date(2026, 11, 20), 2, trace_id="tr-new"),
    ]
    previous = {"payload": {"traces": {"tr-old": {"spans": [{"name": "get_prices"}]}}}}
    cut = snapshot.evidence_cut(snapshot.clean(calls), NOW.date())
    assert snapshot.wanted_traces(cut, previous, NOW) == ["tr-new"]
    doc = snapshot.build(rows(calls), {"tr-new": {"spans": []}}, previous, NOW)
    assert set(doc["payload"]["traces"]) == {"tr-old", "tr-new"}


def test_an_unavailable_summary_is_retried_for_24_hours_only():
    young = datetime(2026, 11, 18, 11, 30)
    old = datetime(2026, 11, 16, 11, 30)
    calls = [
        call(young, date(2026, 11, 19), 1, trace_id="tr-a"),
        call(old, date(2026, 11, 17), 1, trace_id="tr-b"),
    ]
    previous = {"payload": {"traces": {t: {"unavailable": "x"} for t in ("tr-a", "tr-b")}}}
    cut = snapshot.evidence_cut(snapshot.clean(calls), NOW.date())
    assert snapshot.wanted_traces(cut, previous, NOW) == ["tr-a"]


def test_a_payload_over_4_mb_degrades_and_writes():
    big = "x" * 30_000
    calls = []
    for d in range(30):
        a = datetime(2026, 10, 20, 0, 30) + timedelta(days=d)
        calls += [call(a, a.date() + timedelta(days=h), h, rationale=big) for h in range(7)]
    doc = snapshot.build(rows(calls), {}, None, NOW)
    assert doc["degraded"] and snapshot.verify(doc)
    assert len(snapshot._encode(doc)) <= snapshot.MAX_BYTES


def test_still_over_after_degrading_fails():
    huge = "x" * (5 * 1024 * 1024)
    a = datetime(2026, 11, 18, 11, 30)
    with pytest.raises(ValueError, match="without old evidence"):
        snapshot.build(rows([call(a, date(2026, 11, 19), 1, rationale=huge)]), {}, None, NOW)


def test_degrading_has_one_step(caplog):
    """The old second step (null old rationales) acted only on evidence already dropped."""
    huge = "x" * (5 * 1024 * 1024)
    a = datetime(2026, 11, 18, 11, 30)
    with pytest.raises(ValueError):
        snapshot.build(rows([call(a, date(2026, 11, 19), 1, rationale=huge)]), {}, None, NOW)
    assert "dropped evidence older than 7 days" in caplog.text
    assert "rationale" not in caplog.text


def test_history_keeps_the_newest_call_per_date_and_horizon_with_hover_fields():
    """The history chart's dots read confidence and anomaly; /api/calls reads only the newest per date."""
    from tests.ui.export_fixture import document

    hist = document()["payload"]["history"]
    keys = [(c["target_date"], c["horizon_days"]) for c in hist]
    assert len(keys) == len(set(keys))
    assert all({"signal_confidence", "forecast_anomaly_c"} <= set(c) for c in hist)


def _bars(day: date) -> list[dict]:
    open_ = datetime(day.year, day.month, day.day, 14, 30)
    return [
        {
            "ticker": "WMT",
            "bar_start": open_ + timedelta(hours=h),
            "close": 100.0 + h,
            "final": True,
        }
        for h in range(7)
    ]


def test_intraday_keeps_session_calls_at_horizon_0_and_reads_est_when_present():
    d = date(2026, 11, 18)
    calls = [
        call(datetime(2026, 11, 18, 15, 30), d, 0, est_price=101.25, est_reason=None),
        call(datetime(2026, 11, 18, 16, 30), d, 0, est_price=None, est_reason="no_champion"),
        call(datetime(2026, 11, 18, 16, 30), date(2026, 11, 19), 1),  # not horizon 0
        call(datetime(2026, 11, 18, 22, 30), d, 0),  # past the session span
        call(datetime(2026, 11, 17, 15, 30), date(2026, 11, 17), 0),  # no bars that day
    ]
    got = snapshot.build({**rows(calls), "v_ui_intraday": _bars(d)}, {}, None, NOW)["payload"]
    bars, kept = got["intraday"]["bars"], got["intraday"]["calls"]
    assert bars[0]["bar_start"] == "2026-11-18T14:30:00Z" and len(bars) == 7
    assert [(c["as_of"], c["est_price"], c["est_reason"]) for c in kept] == [
        ("2026-11-18T15:30:00Z", 101.25, None),
        ("2026-11-18T16:30:00Z", None, "no_champion"),
    ]
    assert set(kept[0]) == set(snapshot.INTRADAY_CALL_COLUMNS)


def test_intraday_without_bars_is_empty_not_an_error():
    plain = [call(datetime(2026, 11, 18, 15, 30), date(2026, 11, 18), 0)]
    none = snapshot.build(rows(plain), {}, None, NOW)["payload"]["intraday"]
    assert none == {"bars": [], "calls": []}


def test_an_early_close_shows_calls_to_an_hour_past_its_actual_close():
    d, close = date(2026, 11, 27), datetime(2026, 11, 27, 18, 0)
    bars = [{**b, "session_close": close} for b in _bars(d)[:4]]  # 14:30 to 17:30
    calls = [call(datetime(2026, 11, 27, h, 30), d, 0) for h in (15, 18, 19, 21)]
    got = snapshot.build({**rows(calls), "v_ui_intraday": bars}, {}, None, NOW)["payload"]
    assert [c["as_of"][11:16] for c in got["intraday"]["calls"]] == ["15:30", "18:30"]
