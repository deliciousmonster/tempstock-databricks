"""What the proof guards before a destroy: the archive, the lock, the ledger restore and verify."""

import json
import os
import subprocess
import sys

import pytest

from tests.unit.deploy.conftest import FAKE_SECRET
from tests.unit.deploy.proof_world import LEDGER, RAW, SIGNALS_TABLES, _destroys, _stream
from weather_signal.deploy import agent, genie, phases, proof, proof_verify
from weather_signal.deploy.cli import Result
from weather_signal.deploy.lock import Busy

NEW = "yahoo/2026/11/19/06/landed-during-archive.json.gz"


def _lands_after(p, ws, calls):
    """The raw listing gains a file from its `calls`-th call on, as an hourly ingest would."""
    listing, seen = p.ctx.list_volume, []

    def list_volume(root):
        seen.append(root)
        extra = {NEW: 9} if root == RAW and seen.count(RAW) >= calls else {}
        return {**listing(root), **extra}

    p.ctx.list_volume = list_volume
    read = p.read
    p.read = lambda path: b"123456789" if path.endswith(NEW) else read(path)


# Pause, drain, archive, list again; anything new stops the destroy.


def test_a_file_landing_after_the_archive_stops_the_destroy(p, ws):
    _lands_after(p, ws, calls=2)
    with pytest.raises(proof.Stop, match="landed after the archive"):
        proof.run(p)
    assert _destroys(ws) == [] and ws.streamed == []


def test_every_unpaused_dev_schedule_is_paused_before_the_archive(p, ws):
    order = []
    run = p.ctx.run

    def spy(argv, stdin=None):
        if argv[1:3] == ["jobs", "update"]:
            order.append(("pause", json.loads(argv[4])))
        return run(argv, stdin)

    p.ctx.run = spy
    listing = p.ctx.list_volume
    p.ctx.list_volume = lambda root: order.append(("list", root)) or listing(root)
    proof.run(p)
    pauses = [x for k, x in order if k == "pause"]
    assert len(pauses) == 4 and order.index(("pause", pauses[-1])) < order.index(("list", RAW))
    assert all(b["new_settings"]["schedule"]["pause_status"] == "PAUSED" for b in pauses)


def test_an_active_run_is_waited_out_before_the_archive(p, ws):
    ws.active = [{"run_id": 9}]
    slept = []

    def sleep(s):
        slept.append(s)
        ws.active = []

    p.ctx.sleep = sleep
    assert proof.run(p) == 0
    assert slept[0] == 30.0


def test_a_run_that_never_ends_stops_before_the_archive(p, ws):
    ws.active = [{"run_id": 9}]
    with pytest.raises(proof.Stop, match="still active"):
        proof.run(p)
    assert _destroys(ws) == [] and not (p.out / "archive").exists()


# The signals tables go to the archive; a change after the export stops the destroy.


def test_the_signals_tables_are_exported_before_the_destroy(p, ws):
    from weather_signal.deploy import proof_archive

    assert proof_archive.TABLES == SIGNALS_TABLES
    assert proof.run(p) == 0
    lines = (p.out / "archive/tables/signal.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x) for x in lines] == [{"action": "buy", "target_date": "2026-11-20"}]
    assert (p.out / "archive/tables/signal_backtest.jsonl").is_file()


def test_a_row_written_after_the_export_stops_the_destroy(p, ws):
    sql = p.ctx.sql
    exported = []

    def grow(q):
        out = sql(q)
        if q.startswith("SELECT * FROM workspace.dev_signals.agent_runs"):
            exported.append(q)
            ws.rows["workspace.dev_signals.agent_runs"] = [{"run_id": "late"}]
        return out

    p.ctx.sql = grow
    with pytest.raises(proof.Stop, match="agent_runs holds 1 rows, 0 archived"):
        proof.run(p)
    assert exported and _destroys(ws) == []


# A reused --out is refused; an archive file is never overwritten.


@pytest.mark.parametrize("left", ["archive/raw/x", "before.txt", "bootstrap.json"])
def test_a_reused_out_is_refused_before_anything(p, ws, left):
    (p.out / left).parent.mkdir(parents=True, exist_ok=True)
    (p.out / left).write_text("first run", encoding="utf-8")
    with pytest.raises(proof.Stop, match="earlier run"):
        proof.run(p)
    assert ws.writes() == [] and (p.out / left).read_text(encoding="utf-8") == "first run"


# Bootstrap's dev lock, held for the whole run and handed to the bootstrap.


def test_a_held_bootstrap_lock_stops_the_run_before_anything(p, ws):
    with proof.lock.held(proof.lock.path(p.ctx.state_dir, "dev", "dev_")):
        with pytest.raises(proof.Stop, match="holds") as err:
            proof.run(p)
    assert err.value.code == 75 and ws.writes() == []


def test_the_lock_is_held_while_bootstrap_runs_and_handed_to_it(p, ws):
    seen = []
    path = proof.lock.path(p.ctx.state_dir, "dev", "dev_")

    def stream(argv, log, fd):
        with pytest.raises(Busy):
            with proof.lock.held(path):
                pass
        seen.append(os.fstat(fd).st_ino == os.stat(path).st_ino)
        return _stream(ws, argv, log)

    p.stream = stream
    assert proof.run(p) == 0 and seen == [True]
    with proof.lock.held(path):
        pass  # released after the run


def test_a_child_bootstrap_takes_the_lock_its_parent_handed_down(tmp_path):
    path = tmp_path / "bootstrap-dev-dev_.lock"
    code = (
        "import sys; from pathlib import Path; from weather_signal.deploy import lock\n"
        "with lock.bootstrap_lock(Path(sys.argv[1])): print('in')\n"
    )
    with proof.lock.held(path) as fd:
        env = {**os.environ, proof.lock.ENV: str(fd)}
        child = subprocess.run(
            [sys.executable, "-c", code, str(path)], env=env, pass_fds=(fd,),
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        stranger = subprocess.run(
            [sys.executable, "-c", code, str(path)], capture_output=True, text=True, check=False
        )
    assert child.stdout == "in\n", child.stderr
    assert stranger.returncode != 0 and "Busy" in stranger.stderr


# Plan changes no local state either.


def test_plan_keeps_the_genie_record(p, ws):
    genie._remember(p.ctx, json.dumps({"space_id": "g-1"}))
    record = genie._record(p.ctx)
    before = record.read_text(encoding="utf-8")
    assert proof.plan(p) == 0
    assert record.read_text(encoding="utf-8") == before


# Bytes, not sizes; a missing volume is a stop.


def test_a_volume_file_with_the_local_size_but_other_bytes_is_archived(p, ws):
    read = p.read
    p.read = lambda path: b"54321" if path.endswith("local.json.gz") else read(path)
    assert proof.run(p) == 0
    assert (p.out / "archive/raw/yahoo/2026/11/18/21/local.json.gz").read_bytes() == b"54321"


def test_a_missing_volume_the_state_deploys_stops_before_the_destroy(p, ws):
    real = ws._answer
    ws._answer = lambda v, a: (
        Result(1, "", "Error: Volume 'workspace.dev_raw.ledger' does not exist.\n")
        if v == "volumes read" and a[3] == "workspace.dev_raw.ledger"
        else real(v, a)
    )
    with pytest.raises(proof.Stop, match="ledger does not exist"):
        proof.run(p)
    assert _destroys(ws) == []


# The archived ledger is restored before any ingest claims a slot.


def test_bootstrap_restores_the_ledger_and_keeps_a_claim_already_there(ctx, tmp_path):
    src = tmp_path / "ledger"
    (src / "alphavantage/2026-11-19").mkdir(parents=True)
    (src / "alphavantage/2026-11-19/14.json").write_bytes(b"old claim")
    (src / "alphavantage/2026-11-19/15.json").write_bytes(b"old 15")
    ctx.seed_from, ctx.ledger_from = None, src
    ctx.store.files[f"{LEDGER}/alphavantage/2026-11-19/15.json"] = b"new 15"
    ctx.list_volume = lambda root: (
        {k.removeprefix(root + "/"): len(v) for k, v in ctx.store.files.items() if k.startswith(root)}
    )  # fmt: skip
    assert phases.seed_done(ctx) is phases.State.NOT_DONE
    phases.seed_run(ctx)
    assert ctx.store.files[f"{LEDGER}/alphavantage/2026-11-19/14.json"] == b"old claim"
    assert ctx.store.files[f"{LEDGER}/alphavantage/2026-11-19/15.json"] == b"new 15"
    assert phases.seed_done(ctx) is phases.State.DONE


# The agent's profile in the plist; an unreadable app log is a FAIL with its reason.


def test_a_launch_agent_running_as_another_profile_fails_verify(p, ws):
    import plistlib

    from weather_signal.agent import launchd

    path = launchd.plist_path(p.ctx.home)
    doc = plistlib.loads(path.read_bytes())
    doc["EnvironmentVariables"]["WEATHER_SIGNAL_PROFILE"] = "weather-signal"
    path.write_bytes(plistlib.dumps(doc))
    check = proof_verify._launch_agent(p)
    assert not check.ok and "profile 'weather-signal'" in check.detail


def test_an_app_log_that_cannot_be_read_is_a_failed_check_with_its_reason(p, ws):
    ws.rc["apps logs"] = Result(1, "", "Error: permission denied\n")
    serving = proof_verify._app(p)[1]
    assert not serving.ok and "apps logs exit 1: Error: permission denied" in serving.detail


# An interrupt after the rename never deletes the secret the profile now holds.


def test_an_interrupt_after_the_profile_lands_keeps_its_secret(ctx, ws, monkeypatch):
    ctx.cfg_file.write_text("[p]\nhost = x\n", encoding="utf-8")
    ctx.principals = {"agent": "a-1"}
    real = os.replace

    def replace_then_interrupt(a, b):
        real(a, b)
        raise KeyboardInterrupt

    monkeypatch.setattr(agent.os, "replace", replace_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        agent._write_profile(ctx, "https://h")
    assert f"client_secret = {FAKE_SECRET}" in ctx.cfg_file.read_text(encoding="utf-8")
    assert [c for c in ws.calls if c[1:3] == ["service-principal-secrets-proxy", "delete"]] == []


def test_an_interrupt_before_the_rename_still_deletes_the_new_secret(ctx, ws, monkeypatch):
    ctx.cfg_file.write_text("[p]\nhost = x\n", encoding="utf-8")
    ctx.principals = {"agent": "a-1"}

    def interrupt(a, b):
        raise KeyboardInterrupt

    monkeypatch.setattr(agent.os, "replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        agent._write_profile(ctx, "https://h")
    assert [c[3:5] for c in ws.calls if c[1:3] == ["service-principal-secrets-proxy", "delete"]]
