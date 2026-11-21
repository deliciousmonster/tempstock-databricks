"""The LaunchAgent plist: the seven names, minute 40, `run`, and the group kill left on."""

import plistlib
from pathlib import Path

import pytest

from weather_signal.agent.launchd import PROFILES, plist

DBX = "/opt/homebrew/bin"


@pytest.fixture(params=PROFILES)
def doc(request):
    body = plist(
        python="/venv/bin/python",
        profile=request.param,
        databricks_dir=DBX,
        home=Path("/Users/someone"),
        prefix="dev_",
    )
    return request.param, plistlib.loads(body)


def test_schedule_and_program(doc):
    _, d = doc
    assert d["StartCalendarInterval"] == {"Minute": 40}
    assert d["ProgramArguments"] == ["/venv/bin/python", "-m", "weather_signal.agent", "run"]
    assert d.get("AbandonProcessGroup", False) is False
    assert "StartInterval" not in d and "KeepAlive" not in d


def test_environment_exactly_seven_with_the_schema_prefix(doc):
    profile, d = doc
    env = d["EnvironmentVariables"]
    assert env == {
        "CREW_LLM_BACKEND": "plan",
        "CREW_LLM_ROUTE": "databricks-gateway",
        "CREW_DATABRICKS_PROFILE": "weather-signal",
        "CREW_DATABRICKS_SERVICE": "workspace.default.weather_signal_agent",
        "WEATHER_SIGNAL_PROFILE": profile,
        "WEATHER_SIGNAL_SCHEMA_PREFIX": "dev_",
        "PATH": env["PATH"],
    }
    assert not [k for k in env if k.startswith("DATABRICKS_")]
    assert DBX in env["PATH"].split(":")


def test_cwd_and_logs_under_home(doc):
    _, d = doc
    assert d["WorkingDirectory"].endswith("Library/Caches/weather-signal/agent-cwd")
    assert d["StandardErrorPath"].endswith("Library/Logs/weather-signal/agent.log")


def test_unknown_profile_refused():
    with pytest.raises(ValueError, match="profile must be one of"):
        plist(python="p", profile="default", databricks_dir=DBX, home=Path("/h"))


def test_install_writes_the_prefix_it_is_given(monkeypatch, tmp_path, capsys):
    from weather_signal.agent import launchd

    monkeypatch.setattr(launchd.shutil, "which", lambda n: "/opt/bin/databricks")
    monkeypatch.setattr(launchd.Path, "home", lambda: tmp_path)
    assert launchd.main(["--profile", "weather-signal-agent", "--schema-prefix", "dev_"]) == 0
    out = capsys.readouterr().out
    assert "<key>WEATHER_SIGNAL_SCHEMA_PREFIX</key>\n\t\t<string>dev_</string>" in out
    assert not launchd.plist_path(tmp_path).exists()  # printed only, without --yes


def test_install_off_macos_names_the_plist_and_runs_no_launchctl(monkeypatch, tmp_path, capsys):
    from weather_signal.agent import launchd

    ran = []
    monkeypatch.setattr(launchd.shutil, "which", lambda n: "/opt/bin/databricks")
    monkeypatch.setattr(launchd.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(launchd.sys, "platform", "linux")
    monkeypatch.setattr(launchd.subprocess, "run", lambda *a, **k: ran.append(a))
    assert launchd.main(["--profile", "weather-signal-agent", "--yes"]) == 2
    assert ran == [] and not launchd.plist_path(tmp_path).exists()
    assert "macOS only" in capsys.readouterr().err
