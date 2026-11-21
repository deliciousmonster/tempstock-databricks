"""proof-3: a bootstrap finished by hand left verify judging the first attempt, and verify counted
the signal table before the score run it waits for had written it."""

import json
from datetime import timedelta

import pytest

from tests.unit.deploy.proof_world import ENDED, _run, _stream, verified
from weather_signal.deploy import phases, proof, proof_verify
from weather_signal.deploy import proof_checks as pc

RUN = ["run", "--yes", "--profile", "p", "--seed-from", "x", "--out"]
FINISH = ["finish", "--profile", "p", "--seed-from", "x", "--out"]
SIGNAL = "workspace.dev_signals.signal"
FIRST = ENDED - timedelta(hours=3)  # the first attempt, before the score runs it would see
STOPPED = ["preflight: done", "seed: ran", "catchup: stopped: bundle run ingest failed (exit 1):"]


def _first_attempt_failed(p, ws):
    """Run 1's bootstrap stops at catchup."""
    p.ctx.now = lambda: FIRST

    def failed(argv, log, fd=None):
        _stream(ws, argv, log, STOPPED)
        return 1

    p.stream = failed
    assert proof.main([*RUN, str(p.out)], make=lambda a: p) == 1


def _finish(p, ws):
    """`finish` bootstraps again, to the end."""
    p.stream = lambda argv, log, fd=None: _stream(ws, argv, log)
    return proof.main([*FINISH, str(p.out)], make=lambda a: p)


def _failed_then_finished(p, ws):
    """Run 1's bootstrap stops at catchup; `finish` bootstraps again, hours later, to the end."""
    _first_attempt_failed(p, ws)
    p.ctx.now = lambda: ENDED
    return _finish(p, ws)


def _load(p, name):
    return json.loads((p.out / name).read_text(encoding="utf-8"))


def test_finish_bootstraps_again_and_verify_judges_that_bootstrap(p, ws):
    rc = _failed_then_finished(p, ws)
    report = _load(p, "verify.json")
    checks = {c["name"]: c for c in report["checks"]}
    assert rc == 0, [c for c in report["checks"] if not c["ok"]]
    assert checks["bootstrap phases"]["ok"]
    assert _load(p, "bootstrap.json")["rc"] == 0
    assert f"bootstrap ended {_load(p, 'bootstrap.json')['ended']}" in report["notes"]
    assert len(ws.streamed) == 2 and ws.streamed[0] == ws.streamed[1]


def test_finish_keeps_the_failed_attempts_record(p, ws):
    _failed_then_finished(p, ws)
    first = _load(p, "bootstrap-1.json")
    assert first["rc"] == 1 and first["ended"] == FIRST.isoformat()
    assert "catchup: stopped" in (p.out / "bootstrap-1.log").read_text(encoding="utf-8")
    assert "catchup: stopped" not in (p.out / "bootstrap.log").read_text(encoding="utf-8")


def test_finish_refuses_a_folder_whose_bootstrap_never_began(p, ws):
    assert proof.main([*FINISH, str(p.out)], make=lambda a: p) == 2
    assert ws.streamed == [] and not (p.out / "bootstrap.json").exists()


def test_finish_refuses_a_run_that_already_passed_verify(p, ws):
    assert proof.main([*RUN, str(p.out)], make=lambda a: p) == 0
    assert proof.main([*FINISH, str(p.out)], make=lambda a: p) == 2
    assert len(ws.streamed) == 1


# gitfolder is optional: a skip passes the phases check, a stop does not.


def test_a_skipped_gitfolder_passes_the_phases_check():
    lines = [f"{x.name}: ran" for x in phases.PHASES[:-1]]
    lines += ["gitfolder: skipped: repos create says the GitHub account's link has no access",
              "gitfolder: ran"]  # fmt: skip
    assert pc.phases_check("\n".join(lines), 0, [x.name for x in phases.PHASES]).ok


def test_a_stopped_gitfolder_fails_the_phases_check():
    lines = [f"{x.name}: ran" for x in phases.PHASES[:-1]]
    lines += ["gitfolder: stopped: repos create: exit 1: Error: The link to your GitHub account"]
    assert not pc.phases_check("\n".join(lines), 1, [x.name for x in phases.PHASES]).ok


# The signal table is counted once verify has waited for the first scheduled score run.


def _score_runs_during_the_wait(p, ws, rows):
    def scored(_s):
        ws.runs, ws.rows[SIGNAL] = [_run(ENDED, "SUCCESS")], rows

    p.ctx.sleep = scored
    ws.runs, ws.rows[SIGNAL] = [], []


def test_the_signal_rows_the_score_run_wrote_during_the_wait_are_counted(p, ws):
    _stream(ws, ["x"], p.out / "bootstrap.log")
    _score_runs_during_the_wait(p, ws, [{"target_date": "2026-11-20", "action": "buy"}])
    p.write("bootstrap.json", json.dumps({"rc": 0, "began": ENDED.isoformat(),
                                          "ended": ENDED.isoformat()}))  # fmt: skip
    proof_verify.verify(p, wait_s=120)
    checks = {c["name"]: c for c in _load(p, "verify.json")["checks"]}
    assert checks[f"table {SIGNAL}"] == {"name": f"table {SIGNAL}", "ok": True, "detail": "1 rows"}


def test_an_empty_signal_table_after_the_score_run_fails_verify(p, ws):
    rc, checks = verified(p, ws, after=lambda: ws.rows.update({SIGNAL: []}))
    assert rc == 1 and not checks[f"table {SIGNAL}"]["ok"]
    assert checks["first scheduled score run"]["ok"]


# A finish stopped part-way keeps every attempt's record (proof-finish review r1).


def test_a_stop_between_the_set_aside_renames_keeps_the_first_attempts_log(p, ws, monkeypatch):
    _first_attempt_failed(p, ws)
    real = type(p.out).rename

    def stopped_after_one(self, target):
        real(self, target)
        raise KeyboardInterrupt

    monkeypatch.setattr(type(p.out), "rename", stopped_after_one)
    with pytest.raises(KeyboardInterrupt):
        proof.main([*FINISH, str(p.out)], make=lambda a: p)
    monkeypatch.undo()
    assert _finish(p, ws) == 0
    assert "catchup: stopped" in (p.out / "bootstrap-1.log").read_text(encoding="utf-8")
    assert _load(p, "bootstrap-1.json")["rc"] == 1
    assert sorted(x.name for x in p.out.glob("bootstrap*")) == [
        "bootstrap-1.json", "bootstrap-1.log", "bootstrap.json", "bootstrap.log",
    ]  # fmt: skip


def test_a_finish_killed_during_its_bootstrap_leaves_a_started_record(p, ws):
    _first_attempt_failed(p, ws)

    def killed(argv, log, fd=None):
        log.write_text("preflight: done\n", encoding="utf-8")
        raise KeyboardInterrupt

    p.stream = killed
    with pytest.raises(KeyboardInterrupt):
        proof.main([*FINISH, str(p.out)], make=lambda a: p)
    rec = _load(p, "bootstrap.json")
    assert rec["rc"] is None and rec["ended"] is None and rec["began"] == FIRST.isoformat()
    assert proof.main(["verify", "--profile", "p", "--seed-from", "x", "--out", str(p.out)],
                      make=lambda a: p) == 1  # fmt: skip
    assert "recorded no exit" in "\n".join(p.ctx.lines)
    assert _finish(p, ws) == 0
    assert _load(p, "bootstrap-2.json")["rc"] is None
    assert (p.out / "bootstrap-2.log").read_text(encoding="utf-8") == "preflight: done\n"
