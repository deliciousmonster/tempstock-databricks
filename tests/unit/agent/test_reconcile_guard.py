"""One earlier row the table rejects doesn't stop the hour, and stays for the next one."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from weather_signal.agent import hour, runlog
from weather_signal.agent.budget import Budget

CHECK = RuntimeError("[DELTA_VIOLATE_CONSTRAINT_WITH_VALUES] route_valid")


class Rejecting:
    def __init__(self, *bad: str, error: Exception = CHECK):
        self.bad, self.inserted, self.error = set(bad), [], error

    def run_ids_present(self, ids):
        return set()

    def insert_run(self, row):
        if row["run_id"] in self.bad:
            raise self.error
        self.inserted.append(row["run_id"])


def hourly(
    tmp_path: Path,
    *,
    as_of: datetime = datetime(2026, 11, 18, 14, 30, tzinfo=UTC),
    budget: Budget | None = None,
    started_at: datetime | None = None,
) -> hour.Hour:
    return hour.Hour(
        run_id="now",
        as_of=as_of,
        started_at=started_at or datetime.now(UTC),
        hourly=True,
        args=argparse.Namespace(dry_run=False, eval_run=None, fixture=None),
        seams=None,
        cache=tmp_path,
        log_path=tmp_path / runlog.HOURLY,
        budget=budget or Budget(0.0, clock=lambda: 0.0),
    )


def test_a_rejected_row_is_kept_and_the_rest_insert(tmp_path):
    h = hourly(tmp_path)
    for rid in ("bad", "good"):
        runlog.append(h.log_path, {"run_id": rid, "as_of": "x", "outcome": "ok", "route": "none"})
    runlog.append(h.log_path, {"run_id": "now", "line": "started", "route": "none"})
    runlog.deadline_path(tmp_path, "bad").write_text("", encoding="utf-8")
    store = Rejecting("bad")
    hour._reconcile(h, store)
    assert store.inserted == ["good"]
    left = {line["run_id"] for line in runlog.read(h.log_path)}
    assert left == {"bad", "now"}
    assert runlog.deadline_path(tmp_path, "bad").exists()


def test_a_row_refused_for_a_day_is_dropped(tmp_path):
    """A row the table never takes is not retried every hour for ever."""
    h = hourly(tmp_path)
    old = (h.started_at - hour.KEEP_REFUSED - timedelta(minutes=1)).isoformat()
    young = (h.started_at - timedelta(hours=1)).isoformat()
    for rid, at in (("bad", old), ("bad-young", young)):
        runlog.append(
            h.log_path, {"run_id": rid, "started_at": at, "outcome": "ok", "route": "none"}
        )
        runlog.deadline_path(tmp_path, rid).write_text("", encoding="utf-8")
    store = Rejecting("bad", "bad-young")
    hour._reconcile(h, store)
    assert {line["run_id"] for line in runlog.read(h.log_path)} == {"bad-young"}
    assert not runlog.deadline_path(tmp_path, "bad").exists()
    assert runlog.deadline_path(tmp_path, "bad-young").exists()
    dropped = runlog.read(tmp_path / runlog.REFUSED)
    assert [(r["run_id"], r["refused"]) for r in dropped] == [("bad", runlog_error(CHECK))]


def runlog_error(err):
    return f"{type(err).__name__}: {err}"


def test_a_row_held_back_by_an_outage_is_kept_for_days(tmp_path):
    """A laptop asleep over a weekend is not a refusal."""
    h = hourly(tmp_path)
    old = (h.started_at - hour.KEEP_REFUSED * 3).isoformat()
    runlog.append(h.log_path, {"run_id": "away", "started_at": old, "outcome": "ok"})
    runlog.deadline_path(tmp_path, "away").write_text("", encoding="utf-8")
    hour._reconcile(h, Rejecting("away", error=ConnectionError("Connect session lost")))
    assert {line["run_id"] for line in runlog.read(h.log_path)} == {"away"}
    assert runlog.deadline_path(tmp_path, "away").exists()
    assert runlog.read(tmp_path / runlog.REFUSED) == []


def test_a_log_left_with_no_line_is_rewritten_empty(tmp_path):
    """Every row moved aside and no line yet for this run."""
    h = hourly(tmp_path)
    old = (h.started_at - hour.KEEP_REFUSED * 2).isoformat()
    runlog.append(h.log_path, {"run_id": "bad", "started_at": old, "outcome": "ok"})
    hour._reconcile(h, Rejecting("bad"))
    assert h.log_path.read_text(encoding="utf-8") == ""
    assert [r["run_id"] for r in runlog.read(tmp_path / runlog.REFUSED)] == ["bad"]


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("[CAST_OVERFLOW] The value 1e40 of the type DOUBLE cannot be cast"),
        RuntimeError("[DATATYPE_MISMATCH.CAST_WITHOUT_SUGGESTION] cannot resolve"),
        ConnectionError("Connect session lost"),
    ],
)
def test_every_error_class_is_capped_by_age_and_recorded(tmp_path, error):
    """A refusal outside the tokens does not grow the log for ever."""
    h = hourly(tmp_path)
    old = (h.started_at - hour.KEEP_ANY - timedelta(minutes=1)).isoformat()
    week = (h.started_at - hour.KEEP_ANY + timedelta(hours=1)).isoformat()
    for rid, at in (("old", old), ("week", week)):
        runlog.append(h.log_path, {"run_id": rid, "started_at": at, "outcome": "ok"})
    hour._reconcile(h, Rejecting("old", "week", error=error))
    assert {line["run_id"] for line in runlog.read(h.log_path)} == {"week"}
    moved = runlog.read(tmp_path / runlog.REFUSED)
    assert [(r["run_id"], r["refused"]) for r in moved] == [("old", runlog_error(error))]
