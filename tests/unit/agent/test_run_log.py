"""Reconciliation over planted `runs.jsonl` and deadline files."""

import json

from weather_signal.agent.runlog import (
    EVAL,
    HOURLY,
    append,
    deadline_path,
    innermost_main_frame,
    read,
    reconcile,
)

DUMP = """main 0x1f00
Thread 0x2a00 (most recent call first):
  File "/x/threading.py", line 320 in wait
  File "/x/mlflow_upload.py", line 9 in end

Thread 0x1f00 (most recent call first):
  File "/x/socket.py", line 707 in readinto
  File "/x/connect.py", line 88 in read_inputs
  File "/x/worker.py", line 122 in main
"""


def none_present(_ids):
    return set()


def started(rid, **kw):
    return {
        "line": "started",
        "run_id": rid,
        "as_of": "2026-11-18T14:30:00+00:00",
        "email": "x@y",
        **kw,
    }


def test_started_alone_is_crashed(tmp_path):
    log = tmp_path / HOURLY
    append(log, started("a"))
    (row,) = reconcile(log, tmp_path, none_present).rows
    assert (row["run_id"], row["outcome"], row["route"], row["error"]) == (
        "a",
        "crashed",
        "none",
        None,
    )


def test_dump_is_deadline_killed_with_innermost_main_frame(tmp_path):
    log = tmp_path / HOURLY
    append(log, started("b"))
    deadline_path(tmp_path, "b").write_text(DUMP, encoding="utf-8")
    r = reconcile(log, tmp_path, none_present)
    assert r.rows[0]["outcome"] == "deadline_killed"
    assert r.rows[0]["error"] == "socket.py:707 in readinto"
    assert r.deadline_files == [deadline_path(tmp_path, "b")]


def test_empty_dump_file_is_crashed(tmp_path):
    log = tmp_path / HOURLY
    append(log, started("c"))
    deadline_path(tmp_path, "c").write_text("main 0x1f00\n", encoding="utf-8")
    assert reconcile(log, tmp_path, none_present).rows[0]["outcome"] == "crashed"


def test_first_full_line_wins_and_inserts_once(tmp_path):
    log = tmp_path / HOURLY
    append(log, started("d"))
    append(log, {"run_id": "d", "outcome": "ok", "route": "databricks-gateway"})
    append(log, {"run_id": "d", "outcome": "crashed", "route": "none"})
    (row,) = reconcile(log, tmp_path, none_present).rows
    assert (row["outcome"], row["route"]) == ("ok", "databricks-gateway")
    assert "line" not in row


def test_full_line_beats_a_late_dump(tmp_path):
    # The deadline fired between the run line's fsync and the file's unlink.
    log = tmp_path / HOURLY
    append(log, started("e"))
    append(log, {"run_id": "e", "outcome": "partial", "route": "direct_fallback"})
    deadline_path(tmp_path, "e").write_text(DUMP, encoding="utf-8")
    (row,) = reconcile(log, tmp_path, none_present).rows
    assert row["outcome"] == "partial"


def test_present_ids_skipped(tmp_path):
    log = tmp_path / HOURLY
    append(log, started("f"))
    append(log, started("g"))
    assert [r["run_id"] for r in reconcile(log, tmp_path, lambda ids: {"f"}).rows] == ["g"]


def test_eval_log_never_read(tmp_path):
    append(tmp_path / EVAL, started("h"))
    assert reconcile(tmp_path / HOURLY, tmp_path, none_present).rows == []


def test_no_email_on_disk(tmp_path):
    log = tmp_path / HOURLY
    append(log, {**started("i"), "organization": "o"})
    text = log.read_text(encoding="utf-8")
    assert "email" not in text and "organization" not in text
    assert all("email" not in r for r in read(log))
    assert json.loads(text)["run_id"] == "i"


def test_innermost_ignores_other_threads():
    assert (
        innermost_main_frame(DUMP.replace("main 0x1f00", "main 0x2a00"))
        == "threading.py:320 in wait"
    )
    assert innermost_main_frame("no header") is None
