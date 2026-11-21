"""Alpha Vantage ledger: slot claims, the 24-call cap, spacing from `sent_at`, and owed tickers."""

from __future__ import annotations

import bisect
import json
import random
from datetime import date, timedelta

import pytest

from tests.conftest import utc
from tests.unit.av_sim import ROOT, counts, sends, simulate
from tests.unit.fakes import FakeClock, FakeStore
from weather_signal.ingest import av_ledger as av

DAY = timedelta(days=1)


def _run(store, cal, t, call=None):
    clock = FakeClock(t)
    calls = []

    def fake(ticker):
        calls.append(ticker)
        return av.CallResult("landed", date(2026, 11, 16))

    rep = av.run_slot(av.Ledger(ROOT, store), cal, av.Clock(clock.now, clock.sleep), call or fake)
    return rep, calls


def test_hour_claims_its_own_slot(cal):
    store = FakeStore()
    rep, calls = _run(store, cal, utc(2026, 11, 18, 7, 5, 30))
    assert rep.hour == 7 and len(calls) == 1
    assert f"{ROOT}/alphavantage/2026-11-18/07.json" in store.files
    assert f"{ROOT}/alphavantage/2026-11-18/07.result.json" in store.files


@pytest.mark.parametrize("pre", ["exists", "unknown"])
def test_claim_exists_or_unknown_means_no_call(cal, pre):
    store = FakeStore()
    path = f"{ROOT}/alphavantage/2026-11-18/07.json"
    if pre == "exists":
        store.files[path] = b"{}"
    else:
        store.unknown_puts.add(path)
    rep, calls = _run(store, cal, utc(2026, 11, 18, 7, 5, 30))
    assert calls == []
    assert rep.outcome == f"claim_{pre}"


def test_a_25th_slot_is_refused_before_any_request():
    store = FakeStore()
    ledger = av.Ledger(ROOT, store)
    with pytest.raises(av.SlotRefused):
        ledger.claim(date(2026, 11, 18), 24, {"claimed_at": "x"})
    assert store.puts == []
    assert [av.slot_name(h) for h in (0, 23)] == ["00", "23"]


def test_slot_table_puts_spy_and_xlp_after_the_close():
    assert av.SLOT_TICKER == {22: "SPY", 23: "XLP"}


def test_seven_days_at_0530_jitter_send_every_day(cal):
    rng = random.Random(3)
    jitter = {}

    def offset(t):
        return jitter.setdefault(t, 330 + rng.uniform(-30, 30))

    reports, _ = simulate(cal, utc(2026, 12, 1), 24 * 7, offset_s=offset)
    for hour in range(24):
        mine = [r for s, r in reports if s.hour == hour]
        assert [r.outcome for r in mine].count("landed") == 7, hour
        sent = [r.sent_at for r in mine]
        assert all(b - a >= DAY for a, b in zip(sent, sent[1:], strict=False))


def test_late_run_then_not_sent_then_sent_without_wait(cal):
    late = utc(2026, 12, 2, 9)

    def offset(t):
        return 330 + (420 if t == late else 0)

    reports, store = simulate(cal, utc(2026, 12, 1, 9), 24 * 4, offset_s=offset)
    nine = {s.date(): r for s, r in reports if s.hour == 9}
    assert nine[date(2026, 12, 2)].outcome == "landed"
    assert nine[date(2026, 12, 3)].outcome == "not_sent"
    rec = json.loads(store.files[f"{ROOT}/alphavantage/2026-12-03/09.result.json"])
    assert rec["sent_at"] is None and rec["reason"] == "spacing"
    d4 = nine[date(2026, 12, 4)]
    assert d4.outcome == "landed" and d4.sent_at == utc(2026, 12, 4, 9, 5, 30)


def test_missing_result_file_anchors_on_claim_plus_five_minutes(cal):
    def drop(slot, store):
        if slot == utc(2026, 12, 3, 9):
            store.files.pop(f"{ROOT}/alphavantage/2026-12-02/09.result.json")

    def offset(t):
        return 340 if t == utc(2026, 12, 3, 9) else 330

    reports, store = simulate(cal, utc(2026, 12, 2, 9), 25, offset_s=offset, before_slot=drop)
    claim = json.loads(store.files[f"{ROOT}/alphavantage/2026-12-02/09.json"])["claimed_at"]
    r = next(r for s, r in reports if s == utc(2026, 12, 3, 9))
    assert (
        r.sent_at.isoformat()
        == (av.datetime.fromisoformat(claim) + DAY + av.CLAIM_WINDOW).isoformat()
    )


def test_no_24_hour_window_holds_25_calls(cal):
    rng = random.Random(11)
    reports, _ = simulate(cal, utc(2026, 11, 1), 24 * 30, offset_s=lambda t: rng.uniform(300, 420))
    xs = sends(reports)
    assert len(xs) > 24 * 28
    assert max(bisect.bisect_left(xs, x + DAY) - i for i, x in enumerate(xs)) <= 24


@pytest.mark.parametrize(
    "day", [date(2026, 12, 2), date(2026, 7, 15), date(2026, 11, 27), date(2025, 7, 3)]
)
@pytest.mark.parametrize("lag", [0.0, 1.0])
def test_session_days_spend_22_wmt_one_spy_one_xlp(cal, day, lag):
    start = av.slot_start(day - 2 * DAY, 0)
    reports, _ = simulate(cal, start, 24 * 4, lag)
    assert counts(reports, day) == {"WMT": 22, "SPY": 1, "XLP": 1}


def test_saturday_spends_all_24_on_wmt(cal):
    reports, _ = simulate(cal, utc(2026, 12, 3), 24 * 3, 1.0)
    assert counts(reports, date(2026, 12, 5)) == {"WMT": 24, "SPY": 0, "XLP": 0}


def _carried(reports, day):
    return [(s.hour, r.ticker) for s, r in reports if s.date() == day and r.carried]


def test_failed_spy_call_is_carried_on_the_next_wmt_slot(cal):
    reports, _ = simulate(
        cal, utc(2026, 12, 1), 24 * 3, 1.0, fail=frozenset({utc(2026, 12, 2, 22)})
    )
    assert _carried(reports, date(2026, 12, 3))[:1] == [(0, "SPY")]
    assert counts(reports, date(2026, 12, 2)) == {"WMT": 22, "SPY": 1, "XLP": 1}


def test_unknown_result_file_makes_spy_owed(cal):
    def hide(slot, store):
        if slot == utc(2026, 12, 3, 0):
            store.unreadable.add(f"{ROOT}/alphavantage/2026-12-02/22.result.json")

    reports, _ = simulate(cal, utc(2026, 12, 1), 24 * 3, 1.0, before_slot=hide)
    assert _carried(reports, date(2026, 12, 3))[:1] == [(0, "SPY")]


def test_unserved_bar_carries_spy_first_then_turns_three_at_most(cal):
    reports, _ = simulate(cal, utc(2026, 12, 1), 24 * 3, 9.0)
    assert _carried(reports, date(2026, 12, 3)) == [
        (0, "SPY"),
        (1, "XLP"),
        (2, "SPY"),
        (3, "XLP"),
        (4, "SPY"),
        (5, "XLP"),
    ]
    assert counts(reports, date(2026, 12, 3)) == {"WMT": 16, "SPY": 4, "XLP": 4}


def test_failed_xlp_call_is_carried_and_slot_5_stays_wmt(cal):
    reports, _ = simulate(
        cal, utc(2026, 12, 1), 24 * 3, 1.0, fail=frozenset({utc(2026, 12, 2, 23)})
    )
    assert _carried(reports, date(2026, 12, 3))[:1] == [(0, "XLP")]
    assert av.OWN_SLOT == {"SPY": 22, "XLP": 23} and av.SLOT_TICKER.get(5) is None
    assert [r.ticker for s, r in reports if s == utc(2026, 12, 2, 5)] == ["WMT"]
