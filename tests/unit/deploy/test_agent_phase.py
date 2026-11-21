"""Phase agent: the agent's secret into its CLI profile and never anywhere else, then the LaunchAgent."""

import errno
import stat

import pytest

from tests.unit.deploy.conftest import FAKE_SECRET, HOST, agent_installed
from weather_signal.deploy import agent, phases, scope
from weather_signal.deploy.cli import Result, Unknown


def only(*names):
    return tuple(p for p in phases.PHASES if p.name in names)


@pytest.fixture
def fresh_mac(ctx, ws):
    """The owner's profile alone, no LaunchAgent; the fake install writes the plist as install does."""
    ctx.cfg_file.write_text(f"; kept comment\n[p]\nhost = {HOST}\n", encoding="utf-8")
    agent.launchd.plist_path(ctx.home).unlink()
    ctx.principals = {"runner": "r-1", "agent": "a-1"}
    real = ws._answer

    def answer(verb, a):
        if a[1:3] == ["-m", "weather_signal.agent"]:
            text = ctx.cfg_file.read_text(encoding="utf-8")
            agent_installed(ctx)
            ctx.cfg_file.write_text(text, encoding="utf-8")
        return real(verb, a)

    ws._answer = answer
    return ctx


def _creates(ws):
    return [c for c in ws.calls if c[1:3] == ["service-principal-secrets-proxy", "create"]]


def _installs(ws):
    return [c for c in ws.calls if c[1:3] == ["-m", "weather_signal.agent"]]


def test_a_fresh_mac_gets_one_secret_in_the_profile_and_the_agent_installed(fresh_mac, ws):
    assert phases.drive(fresh_mac, only("agent")) == 0
    assert len(_creates(ws)) == 1 and _creates(ws)[0][3] == "12"
    text = fresh_mac.cfg_file.read_text(encoding="utf-8")
    assert text.startswith("; kept comment\n[p]\n")
    assert "[weather-signal-agent]" in text and f"client_secret = {FAKE_SECRET}" in text
    assert f"host = {HOST}\nclient_id = a-1\n" in text
    assert stat.S_IMODE(fresh_mac.cfg_file.stat().st_mode) == 0o600
    assert _installs(ws)[0] == [
        "/venv/bin/python", "-m", "weather_signal.agent", "install",
        "--profile", "weather-signal-agent", "--schema-prefix", "dev_", "--yes",
    ]  # fmt: skip


def test_the_secret_is_in_no_argv_no_stdin_and_no_line(fresh_mac, ws):
    phases.drive(fresh_mac, only("agent"))
    assert not any(FAKE_SECRET in " ".join(c) for c in ws.calls)
    assert not any(s and FAKE_SECRET in s for s in ws.stdin)
    assert not any(FAKE_SECRET in line for line in fresh_mac.lines)


def test_a_done_mac_creates_no_secret_and_installs_nothing(ctx, ws):
    ctx.principals = {"runner": "r-1", "agent": "a-1"}
    assert phases.drive(ctx, only("agent")) == 0
    assert _creates(ws) == [] and _installs(ws) == []
    assert ctx.lines == ["agent: done"]


def test_a_secret_present_but_no_launch_agent_installs_without_a_new_secret(ctx, ws):
    ctx.principals = {"runner": "r-1", "agent": "a-1"}
    agent.launchd.plist_path(ctx.home).unlink()
    assert agent.done(ctx) is False
    agent.run(ctx)
    assert _creates(ws) == [] and len(_installs(ws)) == 1


def test_a_launch_agent_for_another_prefix_is_not_done(ctx):
    ctx.principals = {"runner": "r-1", "agent": "a-1"}
    ctx.scope = scope.build("p", "prod", None, "workspace")
    ctx.live = True
    assert agent.done(ctx) is False


def test_a_section_for_another_agent_is_refused_and_not_overwritten(fresh_mac, ws):
    before = fresh_mac.cfg_file.read_text(encoding="utf-8")
    other = before + "\n[weather-signal-agent]\nhost = https://other\nclient_id = z-9\n"
    fresh_mac.cfg_file.write_text(other, encoding="utf-8")
    assert phases.drive(fresh_mac, only("agent")) == 2
    assert fresh_mac.cfg_file.read_text(encoding="utf-8") == other
    assert _creates(ws) == [] and _installs(ws) == []


def test_a_refused_create_stops_with_the_one_instruction_and_writes_nothing(fresh_mac, ws):
    ws.rc["service-principal-secrets-proxy create"] = Result(1, "", "Error: PERMISSION_DENIED")
    before = fresh_mac.cfg_file.read_text(encoding="utf-8")
    assert phases.drive(fresh_mac, only("agent")) == 2
    assert "Secrets, generate one" in fresh_mac.lines[-1]
    assert fresh_mac.cfg_file.read_text(encoding="utf-8") == before and _installs(ws) == []


def test_a_secret_that_does_not_sign_in_is_unknown_and_never_replaced(ctx, ws):
    ctx.principals = {"runner": "r-1", "agent": "a-1"}
    real = ws._answer
    ws._answer = lambda verb, a: (
        Result(1, "", "") if verb == "agent me"
        else real(verb, a)
    )  # fmt: skip
    with pytest.raises(Unknown):
        agent.done(ctx)
    assert phases.drive(ctx, only("agent")) == 1
    assert _creates(ws) == []


def test_without_the_agent_extra_it_stops_with_the_one_command(fresh_mac, ws):
    fresh_mac.importable = lambda m: m != "claude_agent_sdk"
    assert phases.drive(fresh_mac, only("agent")) == 2
    assert "--extra agent weather-signal bootstrap" in fresh_mac.lines[-1]
    assert _creates(ws) == []


@pytest.mark.parametrize(
    ("target", "prefix", "live"), [("scratch", "t1_", True), ("prod", None, False)]
)
def test_scratch_and_prod_without_live_install_no_agent(fresh_mac, ws, target, prefix, live):
    fresh_mac.scope = scope.build("p", target, prefix, "workspace")
    fresh_mac.live = live
    assert phases.drive(fresh_mac, only("agent")) == 0
    assert _creates(ws) == [] and _installs(ws) == []
    assert fresh_mac.lines[0].startswith("agent: skipped")


def test_a_written_plist_that_launchd_has_not_loaded_is_not_done(ctx, ws):
    ctx.principals = {"runner": "r-1", "agent": "a-1"}
    ws.launched = False
    assert agent.done(ctx) is False


def _deletes(ws):
    return [c for c in ws.calls if c[1:3] == ["service-principal-secrets-proxy", "delete"]]


@pytest.fixture
def full_disk(monkeypatch):
    """The first rename into the config fails as a full disk does; later ones go through."""
    real, failed = agent.os.replace, []

    def replace(src, dst):
        if not failed:
            failed.append(dst)
            raise OSError(errno.ENOSPC, "No space left on device")
        real(src, dst)

    monkeypatch.setattr(agent.os, "replace", replace)
    return failed


def test_a_failed_write_deletes_the_new_secret_and_a_rerun_leaves_one(fresh_mac, ws, full_disk):
    before = fresh_mac.cfg_file.read_text(encoding="utf-8")
    assert phases.drive(fresh_mac, only("agent")) == 1
    assert _deletes(ws) == [
        ["databricks", "service-principal-secrets-proxy", "delete", "12", "s-1", "--profile", "p"]
    ]
    assert fresh_mac.cfg_file.read_text(encoding="utf-8") == before
    assert list(fresh_mac.cfg_file.parent.glob("*.weather-signal.tmp")) == []
    assert "was deleted" in fresh_mac.lines[-1] and FAKE_SECRET not in fresh_mac.lines[-1]
    assert phases.drive(fresh_mac, only("agent")) == 0
    assert len(_creates(ws)) - len(_deletes(ws)) == 1


def test_a_failed_delete_names_the_orphan_and_never_the_secret(fresh_mac, ws, full_disk):
    ws.rc["service-principal-secrets-proxy delete"] = Result(1, "", "Error: denied")
    assert phases.drive(fresh_mac, only("agent")) == 1
    assert "delete secret s-1" in fresh_mac.lines[-1]
    assert not any(FAKE_SECRET in line for line in fresh_mac.lines)


def test_a_symlinked_config_keeps_its_link_and_its_target_gets_the_section(fresh_mac, ws):
    target = fresh_mac.cfg_file.parent / "dotfiles" / "databrickscfg"
    target.parent.mkdir()
    target.write_text(fresh_mac.cfg_file.read_text(encoding="utf-8"), encoding="utf-8")
    fresh_mac.cfg_file.unlink()
    fresh_mac.cfg_file.symlink_to(target)
    assert phases.drive(fresh_mac, only("agent")) == 0
    assert fresh_mac.cfg_file.is_symlink()
    assert "[weather-signal-agent]" in target.read_text(encoding="utf-8")


def test_a_malformed_config_stops_without_quoting_a_line(fresh_mac, ws):
    fresh_mac.cfg_file.write_text(f"[p]\nhost = {HOST}\ntoken-line-fake-0002\n", encoding="utf-8")
    assert phases.drive(fresh_mac, only("agent")) == 2
    assert not any("token-line-fake-0002" in line for line in fresh_mac.lines)
    assert _creates(ws) == []


def test_off_macos_it_stops_before_the_secret_and_names_the_plist(fresh_mac, ws):
    fresh_mac.platform = "linux"
    assert phases.drive(fresh_mac, only("agent")) == 2
    assert _creates(ws) == [] and _installs(ws) == []
    assert not any(c[0] == "launchctl" for c in ws.calls)
    assert str(agent.launchd.plist_path(fresh_mac.home)) in fresh_mac.lines[-1]
