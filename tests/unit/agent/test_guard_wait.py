"""The hour waits for the score job's post-verify instead of holding on the first `v_guard_hold` read."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.unit.agent.test_reconcile_guard import hourly
from weather_signal.agent import hour
from weather_signal.agent.budget import HARD_S, Budget

START_S = 600.0  # the LaunchAgent fires 10 min after the as_of


class Guarded:
    """Inputs at once; the guard verifies after `ok_after` reads, never when `None`."""

    def __init__(self, ok_after: int | None, *, incident: bool = False, start_s: float = START_S):
        self.ok_after, self.incident, self.reads, self.at = ok_after, incident, 0, []
        self.rows: list[dict] = []
        self.clock = [0.0]
        self.start_s = start_s
        self.input_reads = 0
        self.asleep_s: list[float] = []
        self.wall = [0.0]  # wall seconds added, one per sleep, monotonic paused

    def inputs(self, as_of):
        self.input_reads += 1
        return {"2026-11-20": {"signal_action": "hold", "read": self.input_reads}}

    def guard(self, as_of):
        self.reads += 1
        self.at.append(self.clock[0] + self.start_s)
        if self.incident:
            return "incident"
        ok = self.ok_after is not None and self.reads > self.ok_after
        return "ok" if ok else "unverified"

    def writer(self):
        return "fake"

    def insert_run(self, row):
        self.rows.append(row)


def _run(tmp_path: Path, store: Guarded, monkeypatch) -> dict:
    as_of = datetime(2026, 11, 19, 11, 30, tzinfo=UTC)
    budget = Budget(0.0, clock=lambda: store.clock[0])
    h = hourly(
        tmp_path, as_of=as_of, budget=budget, started_at=as_of + timedelta(seconds=store.start_s)
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return as_of + timedelta(seconds=store.start_s + store.clock[0] + store.wall[0])

    def sleep(s):
        store.clock[0] += s
        store.wall[0] += store.asleep_s.pop(0) if store.asleep_s else 0.0

    def wrote(h, store, inputs, row):
        store.explained = inputs
        return hour._end(h, store, {**row, "outcome": "ok"})

    monkeypatch.setattr(hour, "datetime", Clock)
    monkeypatch.setattr(hour, "open_store", lambda h: store)
    monkeypatch.setattr(hour, "_reconcile", lambda h, store: None)
    monkeypatch.setattr(hour.time, "sleep", sleep)
    monkeypatch.setattr(hour, "_explain_and_write", wrote)
    assert hour.run_hour(h) == 0
    return store.rows[-1]


@pytest.mark.parametrize("ok_after", [1, 6])
def test_a_guard_verified_after_some_reads_ends_ok(tmp_path, monkeypatch, ok_after):
    store = Guarded(ok_after)
    assert _run(tmp_path, store, monkeypatch)["outcome"] == "ok"
    assert store.reads == ok_after + 1


def test_a_guard_never_verified_holds_before_the_deadline(tmp_path, monkeypatch):
    store = Guarded(None)
    row = _run(tmp_path, store, monkeypatch)
    assert row["outcome"] == "guard_hold"
    assert store.reads > 1
    assert row["error"] == "v_guard_hold: unverified"
    assert max(store.at) - START_S <= hour.NO_NEW_READ_S < HARD_S


def test_an_open_incident_holds_without_waiting(tmp_path, monkeypatch):
    store = Guarded(0, incident=True)
    assert _run(tmp_path, store, monkeypatch)["outcome"] == "guard_hold"
    assert (store.reads, store.clock[0]) == (1, 0.0)


def test_inputs_are_read_again_once_the_guard_verifies(tmp_path, monkeypatch):
    """A re-score during the wait must not be explained from the old read."""
    store = Guarded(3)
    assert _run(tmp_path, store, monkeypatch)["outcome"] == "ok"
    assert store.explained["2026-11-20"]["read"] == store.input_reads == 2


def test_a_worker_started_at_50_stops_reading_at_55(tmp_path, monkeypatch):
    """The :55 limit binds before the 900 s one for a late worker."""
    store = Guarded(None, start_s=20 * 60)
    row = _run(tmp_path, store, monkeypatch)
    assert row["outcome"] == "guard_hold"
    assert max(store.at) <= hour.POLL_UNTIL_S
    assert store.clock[0] < hour.NO_NEW_READ_S
    assert store.reads == 6  # :50 to :55, one a minute


class Unreadable(Guarded):
    def guard(self, as_of):
        super().guard(as_of)
        if self.reads == 3:
            raise ConnectionError("Connect session lost\nsecond line")
        return "unverified"


def test_a_guard_read_that_raises_holds_the_hour(tmp_path, monkeypatch):
    """Held as `guard_hold` with the reason, not `crashed`."""
    store = Unreadable(None)
    row = _run(tmp_path, store, monkeypatch)
    assert row["outcome"] == "guard_hold"
    assert row["error"] == "v_guard_hold: unreadable (ConnectionError: Connect session lost)"
    assert store.reads == 3


class RereadRaises(Guarded):
    def inputs(self, as_of):
        if self.input_reads == 1:
            self.input_reads += 1
            raise ConnectionError("Connect session lost")
        return super().inputs(as_of)


def test_a_reread_that_raises_holds_the_hour(tmp_path, monkeypatch):
    """The re-read is wrapped like the guard read beside it."""
    row = _run(tmp_path, RereadRaises(1), monkeypatch)
    assert row["outcome"] == "guard_hold"
    assert row["error"] == "inputs re-read: unreadable (ConnectionError: Connect session lost)"


class SlowGuard(Guarded):
    """Each guard read takes `read_s`, so the verify can land past a no-new-read limit."""

    def __init__(self, ok_after: int, *, read_s: float, start_s: float):
        super().__init__(ok_after, start_s=start_s)
        self.read_s = read_s

    def guard(self, as_of):
        state = super().guard(as_of)
        self.clock[0] += self.read_s
        return state


@pytest.mark.parametrize(
    ("ok_after", "read_s", "start_s", "past_900"),
    [
        (5, 110.0, 300.0, True),  # started :35; sixth read verifies at 960 s, before :55
        (1, 150.0, 1200.0, False),  # started :50; second read verifies at :56, 360 s in
    ],
)
def test_no_reread_past_either_limit(tmp_path, monkeypatch, ok_after, read_s, start_s, past_900):
    """A verify that lands late holds rather than reading again."""
    store = SlowGuard(ok_after, read_s=read_s, start_s=start_s)
    row = _run(tmp_path, store, monkeypatch)
    assert (store.clock[0] > hour.NO_NEW_READ_S) is past_900
    assert (store.clock[0] + start_s > hour.POLL_UNTIL_S) is not past_900
    assert (row["outcome"], store.input_reads) == ("guard_hold", 1)
    assert row["error"] == "inputs re-read: verified past the no-new-read limit"


def test_a_mac_asleep_across_55_holds_the_reread(tmp_path, monkeypatch):
    """Monotonic time pauses in sleep; the wall clock decides :55."""
    store = Guarded(1)
    store.asleep_s = [20 * 60.0]  # lid shut after the first read, opened at :61
    row = _run(tmp_path, store, monkeypatch)
    assert store.clock[0] < hour.NO_NEW_READ_S
    assert (row["outcome"], store.input_reads) == ("guard_hold", 1)
    assert row["error"] == "inputs re-read: verified past the no-new-read limit"


def test_the_worker_clock_follows_a_seamed_start(tmp_path):
    """A `--seams` start in the past keeps its offset: `now` is that start plus wall time since."""
    from tests.unit.agent.test_reconcile_guard import hourly as make

    start = datetime(2026, 11, 19, 11, 40, tzinfo=UTC)
    h = make(tmp_path, as_of=start - timedelta(minutes=10), started_at=start)
    h.skew = start - datetime.now(UTC)
    assert abs((h.now() - start).total_seconds()) < 5
