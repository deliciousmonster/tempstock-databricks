"""A destroy that fails part-way is finished by --resume, every stop after the archive
names its ledger, and a fresh run never becomes the recovery once a destroy began."""

import json

import pytest

from tests.unit.deploy.proof_world import _destroys, _state, _stream
from tests.unit.deploy.test_proof_resume_binding import _stopped_after_the_destroy
from weather_signal.deploy import proof
from weather_signal.deploy.cli import Result, not_found

RUN = ["run", "--yes", "--profile", "p", "--seed-from", "x", "--out"]


def _said(p):
    lines = []
    p.ctx.say = lines.append
    return lines


def _partial_destroy(p, ws, times=1):
    """bundle destroy deletes the ledger volume, then fails; the state still lists the rest."""
    inner, left = p.ctx.run, {"fail": times, "ledger_gone": False}

    def run(argv, stdin=None):
        a = list(argv)
        if a[1:3] == ["bundle", "destroy"] and left["fail"] > 0:
            ws.calls.append(a)
            left["fail"] -= 1
            left["ledger_gone"] = True
            return Result(1, "", "Error: app delete timed out\n")
        if (
            a[1:3] == ["volumes", "read"]
            and left["ledger_gone"]
            and a[3].endswith("dev_raw.ledger")
        ):
            return Result(1, "", "Error: Volume does not exist.\n")
        return inner(a, stdin)

    p.ctx.run = run


def _ledger_from(argv_or_line, out):
    return f"--ledger-from {out / 'archive/ledger'}" in argv_or_line


# The reviewer's repro, ported; each printed line now leads somewhere that keeps the ledger.


def test_a_failed_destroy_is_finished_by_the_resume_it_prints(p, ws):
    said = _said(p)
    _partial_destroy(p, ws)
    assert proof.main([*RUN, str(p.out)], make=lambda a: p) == 1
    line = next(x for x in said if "To finish:" in x)
    assert "--resume" in line and f"--out {p.out}" in line
    assert proof.main([*RUN[:2], "--resume", *RUN[2:], str(p.out)], make=lambda a: p) in (0, 1)
    assert len(_destroys(ws)) == 2, "the resume ran the destroy again"
    argv = ws.streamed[-1]
    assert argv[argv.index("--ledger-from") + 1] == str(p.out / "archive/ledger")


def test_a_fresh_run_after_a_failed_destroy_is_refused_and_names_the_resume(p, ws, tmp_path):
    _partial_destroy(p, ws)
    out1 = p.out
    proof.main([*RUN, str(out1)], make=lambda a: p)
    updates = [c for c in ws.calls if c[1:3] == ["jobs", "update"]]
    p.out = tmp_path / "out2"
    with pytest.raises(proof.Stop, match="began a destroy") as err:
        proof.run(p)
    assert f"--resume --profile p --seed-from {p.seed_from} --out {out1}" in str(err.value)
    assert [c for c in ws.calls if c[1:3] == ["jobs", "update"]] == updates, "nothing paused"
    assert len(_destroys(ws)) == 1


def test_a_fresh_run_after_a_failed_bootstrap_names_finish_for_that_run(p, ws, tmp_path):
    def failed(argv, log, fd=None):
        _stream(ws, argv, log)
        ws.state = _state()
        return 1

    p.stream = failed
    out1 = p.out
    proof.main([*RUN, str(out1)], make=lambda a: p)
    p.out = tmp_path / "out2"
    with pytest.raises(proof.Stop, match="began a destroy") as err:
        proof.run(p)
    assert f"proof finish --profile p --seed-from {p.seed_from} --out {out1}" in str(err.value)
    assert "--resume" not in str(err.value)


def test_a_bootstrap_that_exits_0_frees_the_next_fresh_run(p, ws, tmp_path):
    assert proof.main([*RUN, str(p.out)], make=lambda a: p) in (0, 1)
    assert not proof.record.pointer(p).exists()
    ws.state = _state()
    p.out = tmp_path / "out2"
    assert proof.main([*RUN, str(p.out)], make=lambda a: p) in (0, 1)
    assert len(_destroys(ws)) == 2


def test_resume_refuses_a_resource_the_destroy_was_not_planned_for(p, ws):
    _partial_destroy(p, ws)
    proof.main([*RUN, str(p.out)], make=lambda a: p)
    ws.state = _state(**{"resources.jobs.extra": {"state": {"name": "dev_weather-signal-extra"}}})
    with pytest.raises(proof.Stop, match="resources.jobs.extra: deployed, not in the destroy"):
        proof.run(p, resume=True)
    assert len(_destroys(ws)) == 1


def test_any_stop_after_the_archive_names_the_bootstrap_with_its_ledger(p, ws):
    said, real = _said(p), proof.preflight._warehouse

    def cut(ctx):
        raise KeyboardInterrupt

    proof.preflight._warehouse = cut  # teardown's last read before the destroy
    try:
        with pytest.raises(KeyboardInterrupt):
            proof.run(p)
    finally:
        proof.preflight._warehouse = real
    line = next(x for x in said if "To finish:" in x)
    assert _ledger_from(line, p.out) and "--resume" not in line
    assert _destroys(ws) == []


# What counts as deployed, and a teardown with no destroy state.


def test_an_empty_state_is_not_deployed(p, ws):
    ws.state = {"state_version": 1, "state": {}}
    assert proof.deployed_state(p) is None


@pytest.mark.parametrize(
    ("err", "absent"),
    [
        ("Error: Path (/Workspace/x/resources.json) doesn't exist.\n", True),
        ("Error: Job 999 does not exist.\n", True),
        ('{"error_code": "RESOURCE_DOES_NOT_EXIST", "message": "gone"}', True),
        ("Error: Internal error. request id 404c-77ab\n", False),
        ("Error: 503 Service Unavailable: upstream 404 page\n", False),
        ("Error: [TABLE_OR_VIEW_NOT_FOUND] x\n", False),
    ],
)
def test_not_found_reads_the_cli_error_line_or_the_error_code_never_a_bare_404(err, absent):
    assert not_found(Result(1, "", err)) is absent


def test_teardown_without_destroy_state_never_destroys_a_deployed_dev(p, ws):
    assert ws.state is not None
    with pytest.raises(proof.Stop, match="residue"):
        proof.teardown(p, destroy_state=False)
    assert _destroys(ws) == []


def test_resume_refuses_a_dev_deployed_again_after_its_destroy_finished(p, ws):
    _stopped_after_the_destroy(p, ws)
    ws.state = _state()  # deployed by hand, same resources
    with pytest.raises(proof.Stop, match="destroy finished"):
        proof.run(p, resume=True)
    assert len(_destroys(ws)) == 1


def _hand_bootstrap(monkeypatch, p, ledger_from, rc=0, seed_from=None):
    """`weather-signal bootstrap` run by hand, as the proof prints it; its phases end with `rc`."""
    from argparse import Namespace

    from weather_signal.deploy import command

    monkeypatch.setattr(command.phases, "drive", lambda ctx, chosen: rc)
    a = Namespace(destroy=False, write_baseline=None, compare=None, only=None)
    seed = p.seed_from if seed_from is None else seed_from
    return command._run(Namespace(**vars(a), ledger_from=ledger_from, seed_from=seed), p.ctx)


def _bootstrap_fails(p, ws):
    def failed(argv, log, fd=None):
        _stream(ws, argv, log)
        ws.state = _state()
        return 1

    p.stream = failed


def test_a_hand_run_bootstrap_with_the_runs_ledger_frees_the_next_fresh_run(
    p, ws, tmp_path, monkeypatch
):
    """The proof's bootstrap failed, the printed one exited 0 by hand, and
    the pointer it left refused every later fresh run."""
    real = p.stream
    _bootstrap_fails(p, ws)
    out1 = p.out
    proof.main([*RUN, str(out1)], make=lambda a: p)
    assert proof.record.pointer(p).exists()
    assert _hand_bootstrap(monkeypatch, p, out1 / "archive/ledger") == 0
    assert not proof.record.pointer(p).exists()
    p.stream = real
    p.out = tmp_path / "out2"
    assert proof.main([*RUN, str(p.out)], make=lambda a: p) in (0, 1)
    assert len(_destroys(ws)) == 2


@pytest.mark.parametrize(("ledger", "rc"), [("other/ledger", 0), (None, 0), ("run", 1)])
def test_a_hand_run_bootstrap_keeps_the_pointer_unless_it_restored_that_ledger(
    p, ws, tmp_path, monkeypatch, ledger, rc
):
    _bootstrap_fails(p, ws)
    proof.main([*RUN, str(p.out)], make=lambda a: p)
    path = {"run": p.out / "archive/ledger", None: None}.get(ledger, tmp_path / str(ledger))
    _hand_bootstrap(monkeypatch, p, path, rc)
    assert proof.record.pending(p) == p.out


def test_a_hand_bootstrap_whose_ledger_folder_is_gone_keeps_the_pointer(p, ws, monkeypatch):
    """A missing --ledger-from restores nothing and exits 0; the pointer stays."""
    import shutil

    _bootstrap_fails(p, ws)
    proof.main([*RUN, str(p.out)], make=lambda a: p)
    shutil.rmtree(p.out / "archive/ledger")
    _hand_bootstrap(monkeypatch, p, p.out / "archive/ledger")
    assert proof.record.pending(p) == p.out


def test_a_hand_bootstrap_missing_one_archived_ledger_file_keeps_the_pointer(p, ws, monkeypatch):
    _bootstrap_fails(p, ws)
    proof.main([*RUN, str(p.out)], make=lambda a: p)
    files = [f for f in (p.out / "archive/ledger").rglob("*") if f.is_file()]
    assert files, "the fixture archives ledger files"
    files[0].unlink()
    _hand_bootstrap(monkeypatch, p, p.out / "archive/ledger")
    assert proof.record.pending(p) == p.out


def test_a_hand_bootstrap_from_another_data_folder_keeps_the_pointer(p, ws, tmp_path, monkeypatch):
    _bootstrap_fails(p, ws)
    proof.main([*RUN, str(p.out)], make=lambda a: p)
    _hand_bootstrap(monkeypatch, p, p.out / "archive/ledger", seed_from=tmp_path / "other")
    assert proof.record.pending(p) == p.out


def _ledger_record(p, ledger):
    """Rewrite run.json's ledger record (None drops the key) and empty the archived folder."""
    import shutil

    doc = json.loads((p.out / "run.json").read_text())
    doc.pop("ledger")
    doc.update({} if ledger is None else {"ledger": ledger})
    (p.out / "run.json").write_text(json.dumps(doc))
    shutil.rmtree(p.out / "archive/ledger")
    (p.out / "archive/ledger").mkdir()


def test_a_hand_bootstrap_of_a_run_that_archived_no_ledger_frees_the_next_fresh_run(
    p, ws, monkeypatch
):
    """`run.json` recording no ledger files kept the pointer for good, so every fresh
    run was refused and told to rerun the same bootstrap."""
    _bootstrap_fails(p, ws)
    proof.main([*RUN, str(p.out)], make=lambda a: p)
    _ledger_record(p, {})
    _hand_bootstrap(monkeypatch, p, p.out / "archive/ledger")
    assert proof.record.pending(p) is None


def test_a_hand_bootstrap_of_a_run_json_with_no_ledger_record_keeps_the_pointer(p, ws, monkeypatch):
    _bootstrap_fails(p, ws)
    proof.main([*RUN, str(p.out)], make=lambda a: p)
    _ledger_record(p, None)
    _hand_bootstrap(monkeypatch, p, p.out / "archive/ledger")
    assert proof.record.pending(p) == p.out
