"""--resume destroys nothing deployed and binds to its own run; SIGTERM and SIGHUP
print the way back; no agent process outlives the bootout; an ssh session is refused."""

import json
import signal

import pytest

from tests.unit.deploy.proof_world import _destroys, _state, _stream
from weather_signal.deploy import proof, proof_archive

RESUME = ["run", "--yes", "--resume", "--profile", "p", "--seed-from", "x", "--out"]
RUN = ["run", "--yes", "--profile", "p", "--seed-from", "x", "--out"]


def _said(p):
    lines = []
    p.ctx.say = lines.append
    return lines


def _failed_bootstrap(p, ws, redeploys=True):
    """Run 1 destroys and bootstraps; the bootstrap (maybe deploying dev again) exits 1."""

    def stream(argv, log, fd=None):
        _stream(ws, argv, log)
        ws.state = _state() if redeploys else None
        return 1

    p.stream = stream
    proof.main([*RUN, str(p.out)], make=lambda a: p)
    assert len(_destroys(ws)) == 1


# The reviewer's two repros, ported; each now refuses with no second destroy.


def test_resume_after_a_failed_bootstrap_refuses_the_redeployed_dev(p, ws):
    _failed_bootstrap(p, ws)
    before = len(ws.calls)
    with pytest.raises(proof.Stop, match="dev is deployed"):
        proof.run(p, resume=True)
    assert len(_destroys(ws)) == 1
    assert [c for c in ws.calls[before:] if c[1:3] == ["jobs", "update"]] == []


def test_a_failed_bootstrap_names_finish_for_the_same_run_not_resume(p, ws):
    said = _said(p)
    _failed_bootstrap(p, ws)
    line = next(x for x in said if "bootstrap: exit 1" in x)
    assert "--resume" not in line
    assert line.endswith(f"proof finish --profile p --seed-from {p.seed_from} --out {p.out}")


def test_resume_with_the_folder_of_an_earlier_run_refuses_a_live_dev(p, ws):
    assert proof.main([*RUN, str(p.out)], make=lambda a: p) in (0, 1)
    ws.state = _state()  # dev lives again, with data the old archive never saw
    n = len(_destroys(ws))
    assert proof.main([*RESUME, str(p.out)], make=lambda a: p) == 2
    assert len(_destroys(ws)) == n


def test_a_run_whose_verify_passed_is_refused(p, ws):
    assert proof.main([*RUN, str(p.out)], make=lambda a: p) in (0, 1)
    p.write("verify.json", json.dumps({"verdict": "PASS", "checks": [{"ok": True}]}))
    ws.state, streamed = None, len(ws.streamed)
    with pytest.raises(proof.Stop, match="verify passed"):
        proof.run(p, resume=True)
    assert len(ws.streamed) == streamed


def test_resume_after_a_finished_destroy_skips_the_destroy(p, ws):
    _failed_bootstrap(p, ws, redeploys=False)
    p.stream = lambda argv, log, fd=None: _stream(ws, argv, log)
    calls = len(ws.calls)
    proof.run(p, resume=True)
    assert len(_destroys(ws)) == 1
    assert not any(c[1:3] == ["genie", "trash-space"] for c in ws.calls[calls:])
    assert "skipped" in (p.out / "resume.txt").read_text()
    assert len(ws.streamed) == 2


# The folder is bound to the run that archived it.


def test_a_folder_with_only_archive_txt_is_refused(p, ws, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "archive.txt").write_text("raw: 0 files\n")
    ws.schemas, ws.app, ws.state = [], None, None
    p.out = other
    with pytest.raises(proof.Stop, match="no run.json"):
        proof.run(p, resume=True)
    assert ws.streamed == []


def _stopped_after_the_destroy(p, ws):
    real = proof.bootstrap
    proof.bootstrap = lambda q, fd: (_ for _ in ()).throw(KeyboardInterrupt())
    try:
        with pytest.raises(KeyboardInterrupt):
            proof.run(p)
    finally:
        proof.bootstrap = real
    ws.state = None


def test_a_missing_ledger_file_is_refused(p, ws):
    _stopped_after_the_destroy(p, ws)
    doc = json.loads((p.out / "run.json").read_text())
    assert doc["ledger"], "the run archived ledger files"
    victim = next(iter(doc["ledger"]))
    (p.out / "archive/ledger" / victim).unlink()
    with pytest.raises(proof.Stop, match=f"archive/ledger/{victim} is missing"):
        proof.run(p, resume=True)


def test_another_profile_or_data_folder_is_refused(p, ws):
    _stopped_after_the_destroy(p, ws)
    p.seed_from = p.seed_from.parent / "elsewhere"
    with pytest.raises(proof.Stop, match="seed_from"):
        proof.run(p, resume=True)


# SIGTERM and SIGHUP print the way back, as Ctrl-C does.


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_after_the_pause_prints_the_way_back(p, ws, sig):
    said, real = _said(p), proof_archive.archive

    def archive_then_signal(q, deployed):
        assert signal.getsignal(sig) not in (signal.SIG_DFL, signal.SIG_IGN), "no handler installed"
        signal.raise_signal(sig)
        return real(q, deployed)

    before = signal.getsignal(sig)
    proof_archive.archive = archive_then_signal
    try:
        with pytest.raises(proof.Signalled):
            proof.run(p)
    finally:
        proof_archive.archive = real
    assert signal.getsignal(sig) == before
    assert any("To finish:" in x and "bootstrap --profile p --target dev" in x for x in said)
    assert _destroys(ws) == []


# Bootout first, then no agent process before the export.


def test_an_agent_process_that_outlives_the_bootout_is_waited_for(p, ws):
    order, sql, run = [], p.ctx.sql, p.ctx.run
    ws.straggler = 2

    def watch(argv, **kw):
        order.append(argv[0] if argv[0] == "pgrep" else " ".join(argv[:2]))
        return run(argv, **kw)

    p.ctx.run = watch
    p.ctx.sql = lambda q: (
        order.append("export") or sql(q) if q.startswith("SELECT * FROM") else sql(q)
    )
    proof.run(p)
    first_export = order.index("export")
    assert order.index("launchctl bootout") < order.index("pgrep") < first_export
    assert order[:first_export].count("pgrep") == 3
    assert "agent: no agent process after 10 s" in (p.out / "pause.txt").read_text()


def test_an_agent_process_that_never_exits_stops_before_the_export(p, ws):
    ws.straggler = 10_000
    with pytest.raises(proof.Stop, match="agent process is still running"):
        proof.run(p)
    assert not (p.out / "archive").exists()
    assert _destroys(ws) == []


# A session without the gui domain is refused before anything is paused.


def test_an_ssh_session_is_refused_before_the_pause(p, ws):
    ws.gui = False
    with pytest.raises(proof.Stop, match="cannot see gui/"):
        proof.run(p)
    assert [c for c in ws.calls if c[1:3] == ["jobs", "update"]] == []
    assert _destroys(ws) == []


def test_pgrep_matches_only_the_installed_agent_and_ends_its_options(p, ws):
    seen, run = [], p.ctx.run

    def watch(argv, **kw):
        seen.append(argv) if argv[0] == "pgrep" else None
        return run(argv, **kw)

    p.ctx.run = watch
    proof.run(p)
    argv = seen[0]
    assert argv[:3] == ["pgrep", "-f", "--"]
    py = str(p.main / ".venv-connect/bin/python").replace(".", r"\.")
    assert argv[3] == f"^{py} -m weather_signal\\.agent run( |$)"
