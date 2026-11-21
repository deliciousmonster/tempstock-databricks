"""Phase agent: an OAuth secret for `weather-signal-agent` in its CLI profile, then the LaunchAgent.

The profile is staged at mode 0600 before the create, the secret goes from the create's captured
stdout into it, and it is renamed over the config's real file; a failed or interrupted (Ctrl-C)
write deletes the secret.
The secret is never in argv, a line said, or an error.
"""

from __future__ import annotations

import configparser
import json
import os
import plistlib
from pathlib import Path
from typing import Any, TextIO

from weather_signal.agent import launchd
from weather_signal.deploy import preflight
from weather_signal.deploy.cli import Stop, Unknown
from weather_signal.deploy.context import Ctx

PROFILE = launchd.PROFILES[0]
MANUAL = (
    "the workspace would not create a secret for weather-signal-agent (exit {rc}); in Settings > "
    "Identity and access > Service principals > weather-signal-agent > Secrets, generate one and "
    "add [{p}] with host, client_id and client_secret to {cfg}, then rerun"
)


def _sections(ctx: Ctx) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None)
    try:
        if ctx.cfg_file.exists():
            cp.read(ctx.cfg_file, encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError):
        # The parser's text quotes the offending line, which can hold a token.
        raise Stop(
            2, f"{ctx.cfg_file} does not parse as a CLI profile file; fix it and rerun"
        ) from None
    return cp


def _host(ctx: Ctx) -> str:
    cp = _sections(ctx)
    host = (
        cp.get(ctx.scope.profile, "host", fallback="") if cp.has_section(ctx.scope.profile) else ""
    )
    if not host:
        raise Stop(2, f"[{ctx.scope.profile}] in {ctx.cfg_file} has no host")
    return host.rstrip("/")


def _profile(ctx: Ctx) -> dict[str, str] | None:
    cp = _sections(ctx)
    return dict(cp[PROFILE]) if cp.has_section(PROFILE) else None


def _complete(ctx: Ctx, sec: dict[str, str]) -> bool:
    return (
        sec.get("client_id") == ctx.principals["agent"]
        and bool(sec.get("client_secret"))
        and sec.get("host", "").rstrip("/") == _host(ctx)
    )


def _installed(ctx: Ctx) -> bool:
    path = launchd.plist_path(ctx.home)
    if ctx.platform != "darwin" or not path.is_file():
        return False
    doc: dict[str, Any] = plistlib.loads(path.read_bytes())
    env = doc.get("EnvironmentVariables") or {}
    if (doc.get("ProgramArguments") or [""])[0] != ctx.python:
        return False
    if env.get("WEATHER_SIGNAL_PROFILE") != PROFILE:
        return False
    if env.get("WEATHER_SIGNAL_SCHEMA_PREFIX") != ctx.scope.prefix:
        return False
    loaded = ctx.run(["launchctl", "print", f"gui/{os.getuid()}/{launchd.LABEL}"])
    return loaded.rc == 0


def done(ctx: Ctx) -> bool:
    if not ctx.goes_live():
        ctx.say(
            f"agent: skipped on {ctx.scope.target}"
            + (" without --live" if ctx.scope.target == "prod" else "")
        )
        return True
    sec = _profile(ctx)
    if sec is None or not _complete(ctx, sec):
        return False
    me = ctx.run(["databricks", "current-user", "me", "--profile", PROFILE, "-o", "json"])
    if me.rc != 0:
        raise Unknown(
            f"current-user me as {PROFILE}: exit {me.rc}; check its secret in {ctx.cfg_file}"
        )
    return _installed(ctx)


def _sp_id(ctx: Ctx) -> str:
    rows = ctx.cli("service-principals", "list") or []
    ids = [str(r["id"]) for r in rows if r.get("applicationId") == ctx.principals["agent"]]
    if len(ids) != 1:
        raise Unknown(f"{len(ids)} principals with the agent's application id")
    return ids[0]


def _create_secret(ctx: Ctx, sp: str) -> tuple[str, str]:
    """The new secret's id and value."""
    argv = ["databricks", "service-principal-secrets-proxy", "create", sp]
    r = ctx.run([*argv, "--profile", ctx.scope.profile, "-o", "json"])
    if r.rc != 0:
        raise Stop(2, MANUAL.format(rc=r.rc, p=PROFILE, cfg=ctx.cfg_file))
    try:
        doc = json.loads(r.out)
        sid, secret = str(doc.get("id") or ""), doc.get("secret")
    except (json.JSONDecodeError, AttributeError) as err:
        raise Stop(1, "the secret create answered with no JSON; nothing was written") from err
    if not secret:
        raise Stop(1, "the secret create answered with no secret; nothing was written")
    return sid, str(secret)


def _orphan(ctx: Ctx, sp: str, sid: str, why: str) -> str:
    """Delete the secret a failed or interrupted write left with no profile; else name it."""
    argv = ["databricks", "service-principal-secrets-proxy", "delete", sp, sid]
    if sid and ctx.run([*argv, "--profile", ctx.scope.profile]).rc == 0:
        return f"{why}; the new secret {sid} was deleted, so a rerun makes exactly one"
    return (
        f"{why}; delete secret {sid or '(no id returned)'} of weather-signal-agent under Settings > "
        "Identity and access > Service principals > Secrets, then rerun"
    )


def _rebase(f: TextIO, real: Path, staged: str, block: str) -> None:
    """Restage on the config as it is now, so a profile written during the create survives."""
    now = real.read_text(encoding="utf-8") if real.exists() else ""
    if now != staged:
        f.seek(0)
        f.truncate()
        f.write(now.rstrip("\n") + "\n" + block)


def _holds(real: Path, secret: str) -> bool:
    """Whether the config already carries this secret: the rename landed, so it is in use."""
    try:
        return bool(secret) and f"client_secret = {secret}\n" in real.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False  # unreadable: the profile cannot be using it


def _write_profile(ctx: Ctx, host: str) -> None:
    """Stage the file plus one section, mint the secret into it, rename over the real file."""
    real = ctx.cfg_file.resolve()  # a symlinked config keeps its link; its target gets the write
    tmp = real.with_name(real.name + ".weather-signal.tmp")
    block = f"\n[{PROFILE}]\nhost = {host}\nclient_id = {ctx.principals['agent']}\n"
    sp, sid, secret = _sp_id(ctx), None, ""
    try:
        text = real.read_text(encoding="utf-8") if real.exists() else ""
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text.rstrip("\n") + "\n" + block)
            f.flush()
            sid, secret = _create_secret(ctx, sp)
            _rebase(f, real, text, block)
            f.write(f"client_secret = {secret}\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, real)
    except OSError as err:
        if sid is None:
            raise Stop(1, f"could not stage {real} ({err.strerror}); no secret was made") from None
        why = f"could not write {real} ({err.strerror or type(err).__name__})"
        raise Stop(1, _orphan(ctx, sp, sid, why)) from None
    except KeyboardInterrupt:
        if sid is not None and not _holds(real, secret):
            ctx.say(f"agent: {_orphan(ctx, sp, sid, 'interrupted before the profile was written')}")
        raise
    finally:
        tmp.unlink(missing_ok=True)


def run(ctx: Ctx) -> None:
    if ctx.platform != "darwin":
        raise Stop(
            2,
            f"the agent runs under launchd, which is macOS only (this is {ctx.platform}); no secret "
            f"was made. On the Mac its plist is {launchd.plist_path(ctx.home)}: rerun there",
        )
    if not ctx.importable("claude_agent_sdk"):
        raise Stop(2, preflight.connect_line(ctx, "the agent extra (claude_agent_sdk)"))
    sec = _profile(ctx)
    if sec is not None and not _complete(ctx, sec):
        raise Stop(
            2,
            f"[{PROFILE}] in {ctx.cfg_file} is not this workspace's agent; remove it and rerun",
        )
    if sec is None:
        _write_profile(ctx, _host(ctx))
        ctx.say(
            f"agent: created a secret for weather-signal-agent into [{PROFILE}] of {ctx.cfg_file}"
        )
    argv = [ctx.python, "-m", "weather_signal.agent", "install", "--profile", PROFILE]
    r = ctx.run([*argv, "--schema-prefix", ctx.scope.prefix, "--yes"])
    if r.rc != 0:
        raise Stop(1, f"agent install failed (exit {r.rc}):\n{r.tail()}")
