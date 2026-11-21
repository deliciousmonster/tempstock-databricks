"""Rows moved to runs-refused.jsonl can be replayed after a schema fix."""

from __future__ import annotations

import pytest

from weather_signal.agent import refused, runlog


class Table:
    def __init__(self, *still_bad: str):
        self.still_bad, self.inserted = set(still_bad), []

    def run_ids_present(self, ids):
        return {"there"} & set(ids)

    def insert_run(self, row):
        if row["run_id"] in self.still_bad:
            raise RuntimeError("[DELTA_VIOLATE_CONSTRAINT_WITH_VALUES] outcome_valid")
        assert "refused" not in row
        self.inserted.append(row["run_id"])


def test_replay_inserts_what_the_table_now_takes_and_keeps_the_rest(tmp_path):
    path = tmp_path / runlog.REFUSED
    for rid in ("fixed", "still", "there"):
        runlog.append(path, {"run_id": rid, "outcome": "new_kind", "refused": "x"})
    table = Table("still")
    assert refused.replay(tmp_path, table) == (2, 1)
    assert table.inserted == ["fixed"]
    assert [r["run_id"] for r in runlog.read(path)] == ["still"]


def test_replay_with_no_file_does_nothing(tmp_path):
    assert refused.replay(tmp_path, Table()) == (0, 0)
    assert not (tmp_path / runlog.REFUSED).exists()


def test_the_cli_names_the_command(monkeypatch, capsys):
    from weather_signal.agent import __main__ as cli

    seen = []
    monkeypatch.setattr(refused, "main", lambda: seen.append("replay") or 0)
    try:
        cli.main(["replay-refused"])
    except SystemExit as stop:
        assert stop.code == 0
    assert seen == ["replay"]


class Racing(Table):
    """While the first row inserts, an hour moves another row aside, as `_reconcile` does."""

    def __init__(self, cache, *still_bad: str):
        super().__init__(*still_bad)
        self.cache = cache

    def insert_run(self, row):
        if not self.inserted:
            runlog.append(self.cache / runlog.REFUSED, {"run_id": "B", "refused": "y"})
        super().insert_run(row)


def test_a_row_moved_aside_during_the_replay_is_kept(tmp_path):
    """The rewrite once dropped a row appended while it inserted."""
    runlog.append(tmp_path / runlog.REFUSED, {"run_id": "A", "refused": "x"})
    assert refused.replay(tmp_path, Racing(tmp_path)) == (1, 0)
    assert [r["run_id"] for r in runlog.read(tmp_path / runlog.REFUSED)] == ["B"]


def test_the_hour_waits_for_the_replay_rewrite(tmp_path):
    """The hour's move aside and the replay's rewrite take one lock, so neither loses a line."""
    import threading
    from datetime import timedelta

    from tests.unit.agent.test_reconcile_guard import Rejecting, hourly
    from weather_signal.agent import hour

    h = hourly(tmp_path)
    old = (h.started_at - hour.KEEP_REFUSED - timedelta(minutes=1)).isoformat()
    runlog.append(h.log_path, {"run_id": "bad", "started_at": old, "outcome": "ok"})
    with refused.locked(tmp_path):
        t = threading.Thread(target=hour._reconcile, args=(h, Rejecting("bad")))
        t.start()
        t.join(0.5)
        assert t.is_alive()
        assert runlog.read(tmp_path / runlog.REFUSED) == []
    t.join(10)
    assert [r["run_id"] for r in runlog.read(tmp_path / runlog.REFUSED)] == ["bad"]


def test_one_run_id_twice_is_replayed_and_counted_once(tmp_path):
    """A crash between the move and the drop leaves a run_id in the file twice."""
    for rid in ("dup", "dup", "other"):
        runlog.append(tmp_path / runlog.REFUSED, {"run_id": rid, "refused": "x"})
    table = Table()
    assert refused.replay(tmp_path, table) == (2, 0)
    assert table.inserted == ["dup", "other"]


def test_a_bad_or_torn_line_is_quarantined_not_fatal(tmp_path):
    """A non-object line, a line with no run_id and a torn last line stop nothing."""
    path = tmp_path / runlog.REFUSED
    runlog.append(path, {"run_id": "ok", "refused": "x"})
    with path.open("a", encoding="utf-8") as f:
        f.write('[1, 2]\n{"outcome": "ok"}\n{"run_id": "torn", "refu')
    table = Table()
    assert refused.replay(tmp_path, table) == (1, 0)
    assert table.inserted == ["ok"]
    assert runlog.read(path) == []
    bad = (tmp_path / refused.QUARANTINE).read_text(encoding="utf-8").splitlines()
    assert bad == ["[1, 2]", '{"outcome": "ok"}', '{"run_id": "torn", "refu']


def test_an_hour_after_a_torn_line_starts_its_own(tmp_path):
    """A row moved aside after a torn line is not glued onto it."""
    path = tmp_path / runlog.REFUSED
    path.write_text('{"run_id": "torn", "refu', encoding="utf-8")
    refused.append(tmp_path, {"run_id": "next", "refused": "x"})
    assert [r["run_id"] for r in runlog.read(path)] == ["next"]


def test_a_failed_quarantine_append_keeps_the_line_in_the_refused_file(tmp_path):
    """The rewrite once dropped the bad line before its .bad append, so a failed append lost it."""
    path = tmp_path / runlog.REFUSED
    runlog.append(path, {"run_id": "ok", "refused": "x"})
    with path.open("ab") as f:
        f.write(b"[1, 2]\n")
    (tmp_path / refused.QUARANTINE).mkdir()  # the append raises IsADirectoryError
    with pytest.raises(OSError):
        refused.replay(tmp_path, Table())
    assert b"[1, 2]\n" in path.read_bytes()


def test_a_line_with_invalid_utf8_is_quarantined_verbatim(tmp_path):
    path = tmp_path / runlog.REFUSED
    raw = b'{"run_id": "\xff\xfe", "refused": "x"}'
    path.write_bytes(raw + b"\n")
    assert refused.replay(tmp_path, Table()) == (0, 0)
    assert (tmp_path / refused.QUARANTINE).read_bytes() == raw + b"\n"
    assert path.read_bytes() == b""


def test_a_crash_before_the_rewrite_quarantines_a_line_once(tmp_path, monkeypatch):
    """The next replay appended the line the crashed one had quarantined."""
    path = tmp_path / runlog.REFUSED
    runlog.append(path, {"run_id": "ok", "refused": "x"})
    with path.open("ab") as f:
        f.write(b"[1]\n")
    real = refused.os.replace

    def crash(*a):
        raise OSError("crash")

    monkeypatch.setattr(refused.os, "replace", crash)
    with pytest.raises(OSError):
        refused.replay(tmp_path, Table())
    monkeypatch.setattr(refused.os, "replace", real)
    refused.replay(tmp_path, Table())
    assert (tmp_path / refused.QUARANTINE).read_bytes() == b"[1]\n"
