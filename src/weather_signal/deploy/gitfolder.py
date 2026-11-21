"""Phase gitfolder: the repository as a workspace Git folder in the deploying user's home, on a branch.

The folder is user content, not a bundle resource: no destroy or proof removes it, and this phase
never deletes one. No Git credential, a credential whose GitHub link cannot reach the repository, or
a URL or branch the remote does not answer, skips the phase with its reason; a path holding
something else, or a Git folder of another URL, stops it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from weather_signal.deploy.cli import Stop, Unknown, not_found
from weather_signal.deploy.context import Ctx

PROVIDER = "gitHub"
# The Repos API's answer when the credential's GitHub link is not granted the repository.
NO_ACCESS = "the link to your github account does not have access"


@dataclass(frozen=True)
class Folder:
    path: str
    repo_id: str = ""  # empty: nothing at the path yet
    branch: str = ""
    head: str = ""


def repo_name(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def _same_url(a: str, b: str) -> bool:
    def norm(u: str) -> str:
        return u.strip().rstrip("/").removesuffix(".git").lower()

    return norm(a) == norm(b)


def _call(ctx: Ctx, argv: list[str], *, write: bool = False) -> Any:
    """`argv` with the profile, as JSON; a failed write stops, a failed probe is Unknown."""
    r = ctx.run([*argv, "--profile", ctx.scope.profile, "-o", "json"])
    what = " ".join(argv[1:3])
    if r.rc != 0:
        msg = f"{what}: exit {r.rc}: {r.tail(3)}"
        if write and NO_ACCESS in (r.out + r.err).lower():
            raise NoAccess(what)
        raise Stop(1, msg) if write else Unknown(msg)
    try:
        return json.loads(r.out or "null")
    except json.JSONDecodeError as err:
        raise Unknown(f"{what}: output is not JSON") from err


class NoAccess(Exception):
    """A write the credential's GitHub link is not granted; the phase skips, as with no credential."""


def skip_reason(ctx: Ctx) -> str | None:
    """Why the phase cannot run here: no GitHub credential, or one a write found without access."""
    if ctx.git_folder_skip:
        return ctx.git_folder_skip
    creds = ctx.cli("git-credentials", "list") or []
    if any(str(c.get("git_provider", "")).lower().startswith("github") for c in creds):
        return None
    return "the deploying user has no GitHub Git credential in the workspace"


def remote_head(ctx: Ctx) -> str:
    """The branch's head commit on the remote; empty when the remote or the branch did not answer."""
    ref = f"refs/heads/{ctx.git_folder_branch}"
    r = ctx.run(["env", "GIT_TERMINAL_PROMPT=0", "git", "ls-remote", ctx.git_folder_url, ref])
    if r.rc != 0:
        return ""
    return next((x.split()[0] for x in r.out.splitlines() if x.endswith(f"\t{ref}")), "")


def folder(ctx: Ctx) -> Folder:
    """What sits at the path; anything but a Git folder of the configured URL stops the phase."""
    me = str((ctx.cli("current-user", "me") or {}).get("userName") or "")
    if not me:
        raise Unknown("current-user me gave no userName")
    path = f"/Workspace/Users/{me}/{repo_name(ctx.git_folder_url)}"
    argv = ["databricks", "workspace", "get-status", path, "--profile", ctx.scope.profile]
    r = ctx.run([*argv, "-o", "json"])
    if not_found(r):
        return Folder(path)
    if r.rc != 0:
        raise Unknown(f"workspace get-status {path}: exit {r.rc}: {r.tail(3)}")
    try:
        status = json.loads(r.out or "{}")
    except json.JSONDecodeError as err:
        raise Unknown(f"workspace get-status {path}: output is not JSON") from err
    if status.get("object_type") != "REPO":
        raise Stop(1, f"{path} is a {status.get('object_type')}, not a Git folder; move it aside")
    repo = _call(ctx, ["databricks", "repos", "get", str(status["object_id"])])
    if not _same_url(str(repo.get("url") or ""), ctx.git_folder_url):
        raise Stop(
            1,
            f"{path} is a Git folder of {repo.get('url')}, not {ctx.git_folder_url}; "
            "remove or move it in the workspace, then rerun",
        )
    branch, head = str(repo.get("branch") or ""), str(repo.get("head_commit_id") or "")
    return Folder(path, str(repo["id"]), branch, head)


def done(ctx: Ctx) -> bool:
    """On the branch at the remote's head; a skip counts as done, so bootstrap goes on."""
    reason = skip_reason(ctx)
    head = "" if reason else remote_head(ctx)
    if not reason and not head:
        reason = f"{ctx.git_folder_url} gave no {ctx.git_folder_branch} to git ls-remote"
    if reason:
        ctx.say(f"gitfolder: skipped: {reason}")
        return True
    f = folder(ctx)
    return bool(f.repo_id) and f.branch == ctx.git_folder_branch and f.head == head


def run(ctx: Ctx) -> None:
    """Create the folder when the path is free, then check it out; no access skips the phase."""
    try:
        _ensure(ctx)
    except NoAccess as err:
        ctx.git_folder_skip = (
            f"{err} says the GitHub account's link has no access to {ctx.git_folder_url}"
        )


def _ensure(ctx: Ctx) -> None:
    f = folder(ctx)
    repo_id = f.repo_id
    if not repo_id:
        argv = ["databricks", "repos", "create", ctx.git_folder_url, PROVIDER, "--path", f.path]
        repo_id = str(_call(ctx, argv, write=True)["id"])
        ctx.say(f"gitfolder: created {f.path}")
    argv = ["databricks", "repos", "update", repo_id, "--branch", ctx.git_folder_branch]
    _call(ctx, argv, write=True)
    ctx.say(f"gitfolder: {f.path} on {ctx.git_folder_branch}")
