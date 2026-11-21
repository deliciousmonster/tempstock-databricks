"""The evidence cut keeps the previous outlook, so step 2 can open the previous hour's call."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from weather_signal.serving import snapshot


def grid(hours: int, start: datetime) -> list[dict]:
    """Hourly outlooks, each calling the next seven days at horizons 0 to 6."""
    out = []
    for h in range(hours):
        a = start + timedelta(hours=h)
        for k in range(7):
            out.append(
                {
                    "as_of": snapshot.iso(a),
                    "target_date": (a.date() + timedelta(days=k)).isoformat(),
                    "horizon_days": k,
                }
            )
    return out


def test_every_outlook_date_that_existed_an_hour_earlier_has_its_previous_call():
    calls = grid(48, datetime(2026, 11, 16, 0, 30))
    cut = snapshot.evidence_cut(calls, date(2026, 11, 17))
    latest = max(c["as_of"] for c in calls)
    prev = sorted({c["as_of"] for c in calls})[-2]
    outlook = {c["target_date"] for c in calls if c["as_of"] == latest}
    earlier = {c["target_date"] for c in calls if c["as_of"] == prev}
    kept = {c["target_date"] for c in cut if c["as_of"] == prev}
    assert outlook & earlier and (outlook & earlier) <= kept


def test_last_call_per_date_and_horizon_is_kept_for_30_days():
    calls = grid(30, datetime(2026, 11, 1, 0, 30))
    cut = snapshot.evidence_cut(calls, date(2026, 11, 17))
    keys = {(c["target_date"], c["horizon_days"]) for c in cut}
    assert keys == {(c["target_date"], c["horizon_days"]) for c in calls}
    assert len(cut) < len(calls)
