"""A span the source answers well-formed but empty is marked unavailable in the ledger, not failed,
and rechecked 7 days after each check, 3 times at most. proof-3: Previous Runs 2024-01-10..23."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from tests.unit.fakes import FakeStore
from tests.unit.test_catchup import N, put
from tests.unit.test_catchup import cal as catchup_cal
from tests.unit.test_validate_backfill_chunk import prev_body
from weather_signal.ingest import backfill, catchup, unavailable, yahoo_history
from weather_signal.ingest.fetch import RawResponse

PREV = "open-meteo-previous-runs"
HOLE = backfill.Chunk(PREV, date(2024, 1, 10), date(2024, 1, 23), 8)
NOW = datetime(2026, 12, 31, 5, 5, tzinfo=UTC)
MARK = f"{N.ledger}/unavailable/{PREV}/{HOLE.slot}.json"


def empty_day6() -> bytes:
    doc = json.loads(prev_body(HOLE))
    for loc in doc:
        loc["hourly"]["temperature_2m_previous_day6"] = [None] * (HOLE.days * 24)
    return json.dumps(doc).encode()


def seeded() -> FakeStore:
    """Previous Runs landed from its start past every run here, but for the 14 days of HOLE."""
    s = FakeStore()
    put(s, PREV, datetime(2021, 3, 24, tzinfo=UTC), "2021-03-24..2024-01-09", name="a")
    put(s, PREV, datetime(2024, 1, 24, tzinfo=UTC), "2024-01-24..2027-12-31", name="b")
    return s


def run(monkeypatch, s, status=200, body=None, now=NOW):
    """One catch-up; returns its report and the requests it sent."""
    sent = []
    monkeypatch.setattr(yahoo_history, "attempt_for", lambda *a: lambda t: ("valid", "p"))

    def transport(url, params, timeout):
        sent.append(params)
        return RawResponse(status, {}, empty_day6() if body is None else body, now)

    return catchup.run(s, N, transport, catchup_cal(), now, lambda: now, lambda w: None), sent


def mark_of(s) -> dict:
    return json.loads(s.files[MARK])


def test_an_empty_column_is_unavailable_not_invalid_or_failed():
    rep = backfill.Report()
    got = backfill.fetch_checked(HOLE, lambda u, p, t: RawResponse(200, {}, empty_day6(), NOW), rep)
    assert got is None
    assert (rep.invalid, rep.failed) == (0, 0)
    assert rep.unavailable == "temperature_2m_previous_day6 null at every location"


def test_the_span_is_marked_in_the_ledger_and_the_catch_up_does_not_fail(monkeypatch):
    s = seeded()
    rep, sent = run(monkeypatch, s)
    assert len(sent) == 1 and rep.failed == 0 and rep.landed == len(catchup.config.TICKERS)
    assert rep.unavailable == [
        f"{PREV} {HOLE.slot}: temperature_2m_previous_day6 null at every location "
        f"(marked 2026-12-31, rechecked 0 of 3, last 2026-12-31)"
    ]
    assert mark_of(s) == {
        "source": PREV, "slot": HOLE.slot, "marked": "2026-12-31", "last_checked": "2026-12-31",
        "rechecks": 0, "reason": "temperature_2m_previous_day6 null at every location",
    }  # fmt: skip
    assert "unavailable" in rep.__dict__  # the ingest report's own key under catch_up


@pytest.mark.parametrize(
    ("waited", "rechecks", "fetched", "after"),
    [(1, 0, False, 0), (6, 0, False, 0), (7, 0, True, 1), (7, 2, True, 3), (30, 3, False, 3)],
)
def test_a_mark_is_rechecked_7_days_after_its_last_check_3_times_at_most(
    monkeypatch, waited, rechecks, fetched, after
):
    s = seeded()
    run(monkeypatch, s)
    now = NOW + timedelta(days=waited)
    s.files[MARK] = json.dumps({**mark_of(s), "rechecks": rechecks}).encode()
    rep, sent = run(monkeypatch, s, now=now)
    assert (len(sent) == 1) is fetched and rep.failed == 0
    assert mark_of(s)["rechecks"] == after
    assert mark_of(s)["last_checked"] == (now if fetched else NOW).date().isoformat()
    assert mark_of(s)["marked"] == "2026-12-31"
    assert len(rep.unavailable) == 1 and rep.planned == len(sent) + len(catchup.config.TICKERS)


def test_a_recheck_that_lands_clears_the_mark(monkeypatch):
    s = seeded()
    run(monkeypatch, s)
    rep, sent = run(monkeypatch, s, body=prev_body(HOLE), now=NOW + timedelta(days=7))
    assert len(sent) == 1 and rep.failed == 0 and rep.unavailable == []
    assert MARK not in s.files
    assert s.exists(backfill.landing(N.raw, HOLE)) == "present"


@pytest.mark.parametrize(("status", "body"), [(503, b""), (200, b"[1, 2]"), (200, b"not json")])
def test_a_fetch_error_or_malformed_body_still_fails_and_marks_nothing(monkeypatch, status, body):
    s = seeded()
    rep, sent = run(monkeypatch, s, status=status, body=body)
    assert len(sent) == 1 and rep.failed == 1 and rep.unavailable == []
    assert not any("/unavailable/" in p for p in s.files)


def test_a_mark_that_cannot_be_written_fails_the_span(monkeypatch):
    s = seeded()
    s.unknown_puts.add(MARK)
    rep, _ = run(monkeypatch, s)
    assert rep.failed == 1 and rep.unavailable == []
    assert rep.left == [
        f"{PREV} {HOLE.slot}: temperature_2m_previous_day6 null at every location; "
        f"its unavailable mark {MARK} not written"
    ]


def test_a_mark_that_does_not_parse_is_ignored():
    s = FakeStore()
    s.files[MARK] = b"{not json"
    assert unavailable.marks(s, N.ledger, (PREV,)) == {}


def test_a_mark_that_does_not_parse_is_rewritten_not_failed_every_run(monkeypatch):
    s = seeded()
    s.files[MARK] = b"{not json"
    rep, sent = run(monkeypatch, s)
    assert len(sent) == 1 and rep.failed == 0 and rep.left == []
    assert mark_of(s)["last_checked"] == "2026-12-31" and mark_of(s)["rechecks"] == 0
    rep, sent = run(monkeypatch, s, now=NOW + timedelta(days=1))
    assert len(sent) == 0 and rep.failed == 0


@pytest.mark.parametrize("prior", [False, True])
def test_a_mark_an_overlapping_run_wrote_first_is_kept_as_success(monkeypatch, prior):
    s = seeded()
    if prior:  # a recheck: this run deletes the old mark, the other run's lands before its put
        run(monkeypatch, s)
        s.files[MARK] = json.dumps({**mark_of(s), "last_checked": "2026-12-01"}).encode()
    theirs = {
        "source": PREV, "slot": HOLE.slot, "reason": "x", "marked": "2026-12-30",
        "last_checked": "2026-12-30", "rechecks": 1,
    }  # fmt: skip
    real = s.put_if_absent

    def racing(path, data):
        if path == MARK:
            s.files[MARK] = json.dumps(theirs).encode()
        return real(path, data)

    monkeypatch.setattr(s, "put_if_absent", racing)
    rep, sent = run(monkeypatch, s)
    assert len(sent) == 1 and rep.failed == 0 and rep.left == []
    assert mark_of(s) == theirs


@pytest.mark.parametrize("last", ["2026-12-3x", "x" * 40, "2999-01-01"])
def test_a_mark_whose_last_check_does_not_parse_or_is_in_the_future_is_due_now(monkeypatch, last):
    s = seeded()
    run(monkeypatch, s)
    s.files[MARK] = json.dumps({**mark_of(s), "last_checked": last}).encode()
    rep, sent = run(monkeypatch, s, now=NOW + timedelta(days=1))
    assert len(sent) == 1 and rep.failed == 0
    assert mark_of(s)["last_checked"] == "2027-01-01" and mark_of(s)["rechecks"] == 1


def test_a_mark_with_a_field_of_the_wrong_type_is_not_a_mark():
    s = FakeStore()
    doc = {"source": PREV, "slot": HOLE.slot, "reason": "x", "marked": "2026-12-30",
           "last_checked": None, "rechecks": "2"}  # fmt: skip
    s.files[MARK] = json.dumps(doc).encode()
    assert unavailable.marks(s, N.ledger, (PREV,)) == {}
