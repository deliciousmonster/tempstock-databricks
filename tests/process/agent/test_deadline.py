"""The faulthandler deadline: it fires through a blocked or GIL-holding main thread, and never on a clean exit."""

import json
import os
import stat
import time

import pytest

from weather_signal.agent.deadline import BACKSTOP_S
from weather_signal.agent.runlog import HOURLY

DEADLINE_S = 20  # spawn measured at up to 6 s after start at load average 75 to 86


def deadline_files(world):
    return sorted(world.cache.glob("deadline-*.txt"))


@pytest.mark.parametrize(
    ("driver", "frame"),
    [("recv_block", "in stuck"), ("regex_block", "in match")],
    ids=["recv", "gil_regex"],
)
def test_fires_and_next_run_records_it(world, driver, frame):
    world.seams["deadline_s"] = 2
    r = world.run(hourly=True, driver=driver, timeout=30)
    assert r.rc == 1
    assert r.elapsed < 2 + 1 + 4  # interpreter and import time before arming
    (dump,) = deadline_files(world)
    assert "Thread 0x" in dump.read_text()
    assert world.spawns() == 0
    del world.seams["deadline_s"]
    assert world.run(hourly=True).rc == 0
    killed = [x for x in world.table_runs() if x["outcome"] == "deadline_killed"]
    assert len(killed) == 1
    assert frame in killed[0]["error"]
    assert deadline_files(world) == []


def test_clean_hour_exits_past_stuck_threads(world):
    world.seams["deadline_s"] = 30
    r = world.run(hourly=True, driver="stuck_upload", timeout=10)
    assert r.rc == 0
    lines = world.lines(HOURLY)
    assert lines[0]["line"] == "started"
    assert lines[-1]["outcome"] == "ok" and "finished_at" in lines[-1]
    assert deadline_files(world) == []


def test_unwritable_deadline_dir_reads_nothing(world):
    (world.cache / HOURLY).write_text("", encoding="utf-8")
    os.chmod(world.cache, stat.S_IRUSR | stat.S_IXUSR)
    try:
        r = world.run(hourly=True, timeout=30)
    finally:
        os.chmod(world.cache, stat.S_IRWXU)
    assert r.rc == 1
    assert world.spawns() == 0
    assert world.recs() == []
    (started,) = world.lines(HOURLY)
    assert started["line"] == "started"
    assert world.run(hourly=True).rc == 0
    crashed = [x for x in world.table_runs() if x["run_id"] == started["run_id"]]
    assert [c["outcome"] for c in crashed] == ["crashed"]
    assert json.dumps(crashed)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _gone_within(pid, ceiling_s):
    """Poll until `pid` is gone or `ceiling_s` passes; a condition, not a fixed window."""
    end = time.monotonic() + ceiling_s
    while _alive(pid):
        if time.monotonic() > end:
            return False
        time.sleep(0.1)
    return True


def test_group_leader_takes_a_sigterm_deaf_child_with_it(world):
    """(e) As under launchd, the worker leads its group; the fake ignores SIGTERM, as launchd's sends.

    The deadline must fire after the fake has spawned: under load the spawn took up to 6 s, so a 6 s
    deadline sometimes killed the worker first and there was no child to test. The worker may end by
    the dump's `_exit(1)` or, when the dump hangs (measured under load), by the backstop's SIGKILL
    `BACKSTOP_S` later; either way the watchdog must take the group with it.
    """
    world.seams.update(deadline_s=DEADLINE_S, turn_s=300)
    world.cue([{"turn": "hang", "eof_deaf": 600}])
    r = world.run(timeout=DEADLINE_S + BACKSTOP_S + 60, new_session=True)
    assert r.rc == 1 or (r.rc == -9 and r.elapsed >= DEADLINE_S + BACKSTOP_S), r
    pid = next(e["pid"] for e in world.fake() if "spawn" in e)
    gone = _gone_within(pid, 30)
    if not gone:
        os.kill(pid, 9)
    assert gone
