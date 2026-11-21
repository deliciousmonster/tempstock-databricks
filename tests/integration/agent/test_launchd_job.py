"""launchd on this Mac with throwaway labels: one instance at a time, the group killed with the worker,
the `install` plist starting once, and a stuck thread not holding the next hour. About 9 minutes, no model.
"""

import json
import os
import plistlib
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.integration.agent.launchctl import job, loaded
from tests.process.agent.harness import Gateway, World
from weather_signal.agent.deadline import BACKSTOP_S
from weather_signal.agent.launchd import plist

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="launchd")


@pytest.fixture
def mac(live):
    yield
    assert loaded() == [], "a throwaway label was left loaded"


@pytest.fixture
def world(tmp_path):
    gw = Gateway()
    yield World(tmp_path / "w", gw)
    gw.close()


def worker_job(world, cmd, **extra):
    env = {**world.env(hourly=False), "PYTHONPATH": str(Path(__file__).resolve().parents[3])}
    doc = {"ProgramArguments": cmd, "EnvironmentVariables": env, "RunAtLoad": True}
    return job(
        world.root,
        {**doc, "StandardErrorPath": str(world.log), "WorkingDirectory": str(world.root), **extra},
    )


def wait_for(pred, seconds, step=0.5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def test_a_one_instance_at_a_time(mac, tmp_path):
    log = tmp_path / "starts.log"
    script = f'echo "start $$ $(date +%s)" >> {log}; sleep 150; echo "end $$ $(date +%s)" >> {log}'
    with job(
        tmp_path,
        {"ProgramArguments": ["/bin/sh", "-c", script], "StartInterval": 60, "RunAtLoad": True},
    ):
        time.sleep(300)
    events = [line.split() for line in log.read_text().splitlines()]
    running = 0
    for kind, _pid, _t in events:
        running += 1 if kind == "start" else -1
        assert running <= 1
    assert sum(1 for e in events if e[0] == "start") >= 2


def test_b_group_killed_with_the_worker(mac, world):
    world.seams.update(deadline_s=12, turn_s=300)
    world.cue([{"turn": "hang", "eof_deaf": 600}])
    with worker_job(world, world.command(hourly=False)):
        assert wait_for(lambda: world.spawns() == 1, 60)
        fake_pid = next(e["pid"] for e in world.fake() if "spawn" in e)
        ps = ["ps", "-o", "ppid=", "-p", str(fake_pid)]
        worker = int(subprocess.run(ps, capture_output=True, text=True, check=True).stdout)
        # The worker exits by its dump at 12 s, or by the backstop 15 s later if the dump hangs.
        assert wait_for(lambda: not _alive(worker), 12 + BACKSTOP_S + 10, step=0.1)
        gone = wait_for(lambda: not _alive(fake_pid), 3, step=0.1)
        if not gone:
            os.kill(fake_pid, 9)
        assert gone


def test_c_install_plist_starts_once(mac, tmp_path):
    home = tmp_path / "home"
    (home / "Library" / "Caches" / "weather-signal" / "agent-cwd").mkdir(parents=True)
    (home / "Library" / "Logs" / "weather-signal").mkdir(parents=True)
    nxt = datetime.now(UTC) + timedelta(minutes=1)
    doc = plistlib.loads(
        plist(
            python=sys.executable,
            profile="weather-signal",
            databricks_dir="/usr/bin",
            home=home,
            minute=nxt.astimezone().minute,
        )
    )
    doc["Label"] = "dev.weather-signal.agent-test-" + os.urandom(4).hex()
    log = home / "Library" / "Logs" / "weather-signal" / "agent.log"
    with job(tmp_path, doc):
        time.sleep(max(0, (nxt.replace(second=0) - datetime.now(UTC)).total_seconds()) + 20)
    assert log.read_text().count("not under launchd") == 1


def test_d_stuck_thread_does_not_hold_the_next_start(mac, world):
    with worker_job(world, world.command(hourly=False, driver="stuck_upload"), StartInterval=60):
        assert wait_for(
            lambda: sum(1 for x in world.lines() if x.get("line") == "started") >= 2, 130, step=2
        )
    full = [x for x in world.lines() if x.get("line") != "started"]
    assert full and all(x["outcome"] == "ok" for x in full)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_labels_clean():
    assert loaded() == []
    assert json.dumps(loaded()) == "[]"
