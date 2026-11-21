"""`check`: one incident row and notification per bad version, one failure reason, ack bounds, metrics."""

import time
from datetime import date, timedelta

import pytest

from tests.conftest import utc
from tests.unit.guard_world import RECS, SIG, SPECS, rec_row, world
from weather_signal.guard import check, repair
from weather_signal.signal import checks as cr

AS_OF = utc(2026, 12, 2, 12, 30)


def test_one_incident_row_and_one_notification_over_six_runs_then_a_reminder():
    cat = world()
    cat.commit(SIG, "UPDATE", {}, user="intruder")
    notes = []
    for h in range(6):
        t = AS_OF + timedelta(hours=h)
        check.run(cat, SPECS.values(), t, "pre", t, notes.append)
    assert len(cat.incident_rows) == 1 and len(notes) == 1
    t = AS_OF + timedelta(hours=24)
    check.run(cat, SPECS.values(), t, "pre", t, notes.append)
    assert len(notes) == 2 and "still open" in notes[-1] and len(cat.incident_rows) == 1


def test_failed_guard_reports_guard_incident_only():
    cat = world()
    cat.commit(SIG, "DELETE", {}, user="intruder")
    outcomes = check.run(cat, SPECS.values(), AS_OF, "pre", AS_OF, lambda m: None)
    assert check.failure_reason(outcomes, signal_rows_ok=False) == "guard_incident"
    clean = check.run(world(), SPECS.values(), AS_OF, "pre", AS_OF, lambda m: None)
    assert check.failure_reason(clean, signal_rows_ok=False) == "no_signal_rows"


def test_ack_bounds():
    cat = world()
    cat.insert(RECS, [rec_row(1)])
    cat.commit(RECS, "UPDATE", {}, user="intruder")
    cat.insert(RECS, [rec_row(2)])
    check.run(cat, SPECS.values(), AS_OF, "pre", AS_OF, lambda m: None)
    latest, bad = cat.latest_version(RECS), cat.guard[RECS].last_bad_version
    with pytest.raises(repair.RepairRefused):
        repair.ack(cat, SPECS[RECS], latest + 1)
    with pytest.raises(repair.RepairRefused):
        repair.ack(cat, SPECS[RECS], bad - 1)
    assert repair.ack(cat, SPECS[RECS], latest) == "acknowledged"


def test_tamper_on_signal_holds_the_agent_though_recommendations_is_clean():
    cat = world()
    check.run(cat, SPECS.values(), AS_OF, "post", AS_OF, lambda m: None)
    assert check.may_write(cat.guard_rows(), AS_OF, "post")
    cat.commit(SIG, "UPDATE", {}, user="intruder")
    t = AS_OF + timedelta(hours=1)
    check.run(cat, SPECS.values(), t, "post", t, lambda m: None)
    assert not cat.guard[RECS].open_incident and cat.guard[RECS].post_verified_as_of == t
    assert not check.may_write(cat.guard_rows(), t, "post")


def test_disagreements_and_calendar_end():
    assert cr.disagreements(6)[0].level == "fail" and cr.disagreements(5) == []
    assert cr.calendar_end(date(2035, 7, 5), date(2035, 12, 31))[0].level == "warn"
    assert cr.calendar_end(date(2026, 12, 2), date(2035, 12, 31)) == []
    assert cr.verdict(cr.calendar_end(date(2035, 7, 5), date(2035, 12, 31))) == "warn"


def _summary(n_metros, age, naive=False):
    fetched = utc(2026, 11, 18, 3) - age
    return {
        "path": "p",
        "lead_days": 7,
        "n_metros": n_metros,
        "hours": 6 * n_metros,
        "fetched_at": fetched.replace(tzinfo=None) if naive else fetched,
    }


def _filled(row):
    return [(f.level, f.name) for f in cr.forecast_filled([row], utc(2026, 11, 18, 3))]


def test_a_lead_quarantined_at_five_metros_fails_the_check_and_four_do_not():
    assert _filled(_summary(4, timedelta(0))) == []
    assert _filled(_summary(5, timedelta(0))) == [("fail", "forecast_filled")]


def test_the_fill_alert_looks_back_seven_days():
    assert _filled(_summary(50, timedelta(days=6, hours=23))) == [("fail", "forecast_filled")]
    assert _filled(_summary(50, timedelta(days=7, hours=1))) == []


def test_a_naive_fetched_at_from_spark_is_read_as_utc(monkeypatch):
    """Under a non-UTC process zone, so reading naive as local time fails."""
    monkeypatch.setenv("TZ", "America/Denver")
    time.tzset()
    assert _filled(_summary(50, timedelta(days=6, hours=23), naive=True)) == [
        ("fail", "forecast_filled")
    ]
    assert _filled(_summary(50, timedelta(days=7, hours=1), naive=True)) == []
    monkeypatch.undo()
    time.tzset()


def test_an_envelope_already_alerted_on_is_not_alerted_again():
    row = _summary(50, timedelta(hours=1))
    assert cr.forecast_filled([row], utc(2026, 11, 18, 3), alerted={"p"}) == []
    assert cr.forecast_filled([row], utc(2026, 11, 18, 3), alerted={"other"}) != []
