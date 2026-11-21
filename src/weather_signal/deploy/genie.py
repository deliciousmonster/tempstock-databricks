"""The Genie space, made after `setup`: Genie refuses a space over views that do not exist yet.

As a bundle resource it was created at the first deploy, before any view, and failed (measured on
dev, 2026-11-19: 403 "Table ... v_ui_agent_runs does not exist"). It is found again by its title,
or, inside the list lag, by the id the create returned, kept in the state folder for 15 minutes.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from weather_signal.deploy.cli import Result, Stop, Unknown, not_found
from weather_signal.deploy.context import Ctx

SPACE = "aibi/weather_signal.genie.json"
LIST_WAIT_S, LIST_EVERY_S = 60.0, 3.0  # a new space lists some seconds after its create
RECORD_TRUST_S = 900.0  # a recorded create counts only this long; after it the list decides
DESCRIPTION = "Ask the weather-signal views in plain words. Personal demo, not investment advice."


def title(ctx: Ctx) -> str:
    return f"{ctx.scope.prefix}weather-signal"


def serialized(repo: Path, app_schema: str) -> str:
    """aibi/weather_signal.genie.json with its tables in `catalog.schema`, compact as the API stores it."""
    text = (
        (repo / SPACE).read_text(encoding="utf-8").replace("${catalog}.${app_schema}", app_schema)
    )
    return json.dumps(json.loads(text), separators=(",", ":"), sort_keys=True)


def spaces(ctx: Ctx) -> list[dict[str, Any]]:
    """Every space the caller sees, page by page; a page that does not answer is Unknown."""
    out: list[dict[str, Any]] = []
    token = ""
    while True:
        page = ctx.cli("genie", "list-spaces", *(["--page-token", token] if token else [])) or {}
        out += page.get("spaces") or []
        token = page.get("next_page_token") or ""
        if not token:
            return out


def _record(ctx: Ctx) -> Path:
    return ctx.state_dir / f"genie-{ctx.scope.target}-{ctx.scope.prefix}.json"


def forget(ctx: Ctx) -> None:
    _record(ctx).unlink(missing_ok=True)


def _recorded(ctx: Ctx, tidy: bool) -> list[str]:
    """The space this machine created that may not list yet: a fresh record get-space confirms.
    `tidy` drops a record that no longer counts; a read-only caller passes False."""
    drop = forget if tidy else (lambda c: None)
    try:
        doc = json.loads(_record(ctx).read_text(encoding="utf-8"))
        sid, made = str(doc["space_id"]), datetime.fromisoformat(doc["created_at"])
    except FileNotFoundError:
        return []
    except (OSError, ValueError, KeyError, TypeError):
        drop(ctx)
        return []
    if (ctx.now() - made).total_seconds() > RECORD_TRUST_S:
        drop(ctx)  # past the lag the list is the truth; a trashed space must not count
        return []
    r = ctx.run(
        ["databricks", "genie", "get-space", sid, "--profile", ctx.scope.profile, "-o", "json"]
    )
    if not_found(r):
        drop(ctx)
        return []
    if r.rc != 0:
        raise Unknown(f"genie get-space {sid}: exit {r.rc}: {r.tail(3)}")
    try:
        same = json.loads(r.out).get("title") == title(ctx)
    except (json.JSONDecodeError, AttributeError) as err:
        raise Unknown(f"genie get-space {sid}: output is not JSON") from err
    return [sid] if same else []


def found(ctx: Ctx, tidy: bool = True) -> list[str]:
    """Listed spaces with this title; inside the list lag, the one this machine just created."""
    listed = [str(s["space_id"]) for s in spaces(ctx) if s.get("title") == title(ctx)]
    if listed:
        if tidy:
            forget(ctx)
        return listed
    return _recorded(ctx, tidy)


def _ours(ctx: Ctx) -> str:
    return serialized(ctx.repo, ctx.scope.names.schema("app"))


def _post(ctx: Ctx, argv: list[str], body: dict[str, Any]) -> Result:
    """A genie write with its body in a file, since the serialized space outgrows argv."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8") as f:
        json.dump(body, f)
        f.flush()
        return ctx.run(
            [*argv, "--json", f"@{f.name}", "--profile", ctx.scope.profile, "-o", "json"]
        )


def _space(ctx: Ctx, sid: str) -> tuple[str, Any]:
    """The space's description and its serialized instructions, from get-space."""
    argv = ["databricks", "genie", "get-space", sid, "--include-serialized-space"]
    r = ctx.run([*argv, "--profile", ctx.scope.profile, "-o", "json"])
    if r.rc != 0:
        raise Unknown(f"genie get-space {sid}: exit {r.rc}: {r.tail(3)}")
    try:
        doc = json.loads(r.out)
        return str(doc.get("description") or ""), json.loads(doc["serialized_space"]).get(
            "instructions"
        )
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as err:
        raise Unknown(f"genie get-space {sid}: no serialized space in its output") from err


def _described(ctx: Ctx, sid: str) -> str:
    """The space's description, from get-space without its serialized space."""
    r = ctx.run(
        ["databricks", "genie", "get-space", sid, "--profile", ctx.scope.profile, "-o", "json"]
    )
    if r.rc != 0:
        raise Unknown(f"genie get-space {sid}: exit {r.rc}: {r.tail(3)}")
    try:
        return str(json.loads(r.out).get("description") or "")
    except (json.JSONDecodeError, AttributeError) as err:
        raise Unknown(f"genie get-space {sid}: output is not JSON") from err


def current(ctx: Ctx, sid: str) -> bool:
    """The space's instructions are the repo's aibi/weather_signal.genie.json ones."""
    return bool(_space(ctx, sid)[1] == json.loads(_ours(ctx)).get("instructions"))


def done(ctx: Ctx) -> bool:
    ids = found(ctx)
    return bool(ids) and all(current(ctx, sid) for sid in ids)


def ensure(ctx: Ctx) -> None:
    """Create the space, or replace an existing one's serialized space when its instructions
    are not the repo's. Only a space carrying DESCRIPTION, which create sets, is replaced."""
    ids = found(ctx)
    if not ids:
        create(ctx)
    for sid in ids:
        described, instructions = _space(ctx, sid)
        if instructions == json.loads(_ours(ctx)).get("instructions"):
            continue
        if described != DESCRIPTION:
            raise Stop(1, f"genie: space {sid} is titled {title(ctx)} but bootstrap did not make "
                          "it (another description); left as it is. Trash it to have one made")  # fmt: skip
        r = _post(
            ctx, ["databricks", "genie", "update-space", sid], {"serialized_space": _ours(ctx)}
        )
        if r.rc != 0:
            raise Stop(1, f"genie update-space {sid} failed (exit {r.rc}):\n{r.tail()}")


def create(ctx: Ctx) -> None:
    body = {
        "warehouse_id": ctx.warehouse_id,
        "serialized_space": _ours(ctx),
        "title": title(ctx),
        "description": DESCRIPTION,
    }
    r = _post(ctx, ["databricks", "genie", "create-space"], body)
    if r.rc != 0:
        raise Stop(1, f"genie create-space failed (exit {r.rc}):\n{r.tail()}")
    _remember(ctx, r.out)
    waited = 0.0
    while not found(ctx):
        if waited >= LIST_WAIT_S:
            raise Stop(1, f"genie: {title(ctx)} created but not listed after {LIST_WAIT_S:.0f} s")
        ctx.sleep(LIST_EVERY_S)
        waited += LIST_EVERY_S


def _remember(ctx: Ctx, out: str) -> None:
    """Record the new space's id before anything can stop the run, so a rerun finds it."""
    try:
        sid = str(json.loads(out or "{}").get("space_id") or "")
    except (json.JSONDecodeError, AttributeError):
        sid = ""
    if not sid:
        return
    path = _record(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"space_id": sid, "created_at": ctx.now().isoformat()}
    path.write_text(json.dumps(doc) + "\n", encoding="utf-8")


def trash(ctx: Ctx) -> list[str]:
    """Trash every space with this scope's title, and its record; the ids trashed. Only a space
    carrying DESCRIPTION is trashed: any other stops the trash before it deletes anything."""
    ids, out = found(ctx), []
    foreign = [sid for sid in ids if _described(ctx, sid) != DESCRIPTION]
    if foreign:
        raise Stop(1, f"genie: {', '.join(foreign)} titled {title(ctx)} but bootstrap did not "
                      "make it (another description); nothing trashed. Rename or trash it by hand")  # fmt: skip
    for space in ids:
        t = ctx.run(["databricks", "genie", "trash-space", space, "--profile", ctx.scope.profile])
        if t.rc != 0:
            raise Stop(1, f"genie trash-space {space} failed (exit {t.rc}):\n{t.tail()}")
        out.append(space)
    forget(ctx)
    return out
