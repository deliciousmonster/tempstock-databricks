"""The hourly path runs only under its launchd label; `--as-of` runs anywhere and writes only the eval log."""

from datetime import timedelta

import pytest

from tests.process.agent.harness import AS_OF
from weather_signal.agent.runlog import EVAL, HOURLY


@pytest.mark.parametrize("label", [None, "dev.weather-signal.other"])
def test_refused_off_label(world, label):
    env = {"XPC_SERVICE_NAME": label} if label else {}
    world.seams["now"] = (AS_OF + timedelta(minutes=10)).isoformat()
    cmd_env = {**world.env(hourly=False), **env}
    import subprocess

    p = subprocess.run(
        world.command(hourly=True),
        env=cmd_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert p.returncode == 2
    assert "launchctl kickstart" in p.stderr
    assert not (world.cache / HOURLY).exists()
    assert world.spawns() == 0


def test_runs_under_label(world):
    assert world.run(hourly=True).rc == 0
    assert world.lines(HOURLY)[-1]["outcome"] == "ok"
    assert not (world.cache / EVAL).exists()


def test_as_of_runs_off_label_and_skips_late_check(world):
    world.seams["now"] = (AS_OF + timedelta(hours=5)).isoformat()
    assert world.run().rc == 0
    assert world.lines(EVAL)[-1]["outcome"] == "ok"
    assert not (world.cache / HOURLY).exists()


def test_late_start(world):
    world.seams["now"] = (AS_OF + timedelta(minutes=25)).isoformat()
    assert world.run(hourly=True).rc == 0
    assert world.lines(HOURLY)[-1]["outcome"] == "late_start"
    assert world.spawns() == 0
