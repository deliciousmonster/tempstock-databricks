"""A fake workspace behind the `databricks` CLI, and a Ctx wired to it; no process is spawned."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.unit.fakes import FakeStore
from weather_signal import config
from weather_signal.agent import launchd
from weather_signal.deploy import genie, phases, scope, wheel
from weather_signal.deploy.cli import Result
from weather_signal.deploy.context import Ctx

REPO = Path(__file__).parents[3]
WRITES = (
    ("bundle", "deploy"),
    ("bundle", "run"),
    ("bundle", "destroy"),
    ("jobs", "update"),
    ("service-principals", "create"),
    ("secrets", "put-secret"),
    ("genie", "create-space"),
    ("genie", "update-space"),
    ("genie", "trash-space"),
    ("service-principal-secrets-proxy", "create"),
    ("service-principal-secrets-proxy", "delete"),
    ("-m", "weather_signal.agent"),
    ("repos", "create"),
    ("repos", "update"),
    ("workspace", "delete"),
)
HEAD = "c0ffee" * 6 + "c0ff"
# The Git folder as `repos get` returns it, on main at the remote's head.
FOLDER = {
    "object_type": "REPO", "id": 77, "url": config.GIT_FOLDER_URL,
    "branch": config.GIT_FOLDER_BRANCH, "head_commit_id": HEAD,
}  # fmt: skip
# dev's space as get-space returns it: the repo's serialized space, key for key (measured).
SPACE = {
    "space_id": "g-1",
    "title": "dev_weather-signal",
    "description": genie.DESCRIPTION,
    "serialized_space": genie.serialized(REPO, "workspace.dev_app"),
}
HOST = "https://example.cloud.databricks.com"
FAKE_SECRET = "fake-secret-value-0001"


def ok(doc: Any) -> Result:
    return Result(0, json.dumps(doc), "")


class Workspace:
    """Every probe answers from these fields; a key in `broken` answers exit 1 with no text."""

    def __init__(self) -> None:
        self.principals = [
            {"displayName": "weather-signal-runner", "applicationId": "r-1", "id": "11"},
            {"displayName": "weather-signal-agent", "applicationId": "a-1", "id": "12"},
        ]
        self.schemas = ["dev_raw", "dev_daily", "dev_signals", "dev_ops"]
        self.secrets = [{"key": config.SECRET_KEY}]
        self.plan: dict[str, Any] = {
            "plan_version": 2,
            "plan": {"resources.jobs.score": {"action": "skip"}},
        }
        self.tables = {
            s: sorted(t for sch, t in phases.pipeline_tables(REPO) if sch == s)
            for s in ("raw", "daily", "signals")
        }
        self.app: dict[str, Any] | None = {
            "name": "dev-weather-signal-ui",
            "service_principal_client_id": "app-1",
            "active_deployment": {"status": {"state": "SUCCEEDED"}},
        }
        self.spaces = [dict(SPACE)]
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.created: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self.list_lag = 0
        self.warehouses = [{"id": "wh-1", "name": "Serverless Starter Warehouse"}]
        self.metadata: str | None = None
        self.broken: set[str] = set()
        self.calls: list[list[str]] = []
        self.stdin: list[str | None] = []
        self.rc: dict[str, Result] = {}
        self.launched = True
        self.agent_running = False
        self.gui = True  # launchctl sees gui/<uid>: false from an ssh session
        self.straggler = 0  # pgrep calls that still find an agent process after the bootout
        self.jobs: dict[str, str] = {}  # deployed job name -> its schedule's pause_status
        self.git_credentials = [{"credential_id": 1, "git_provider": "gitHubOAuth"}]
        self.remote_head: str | None = HEAD  # None: git ls-remote does not answer
        self.git_folder: dict[str, Any] | None = dict(FOLDER)  # what sits at the folder's path
        self.artifacts: dict[str, dict[str, Any]] | None = None  # path -> its listing row

    def writes(self) -> list[list[str]]:
        return [c for c in self.calls if tuple(c[1:3]) in WRITES]

    def __call__(self, argv: Sequence[str], stdin: str | None = None) -> Result:
        a = list(argv)
        self.calls.append(a)
        self.stdin.append(stdin)
        verb = " ".join(a[1:3])
        if verb in self.broken:
            return Result(1, "", "")
        if verb in self.rc:
            return self.rc[verb]
        if verb == "jobs update":
            return self._update(a)
        if verb == "jobs list" and "--name" in a:
            return self._jobs(a[a.index("--name") + 1])
        if verb == "genie get-space":
            hit = [s for s in self.spaces + self.pending if s["space_id"] == a[3]]
            return ok(hit[0]) if hit else Result(1, "", "Error: Space does not exist.\n")
        if a[0] in ("launchctl", "pgrep"):
            return self._launchctl(a)
        if a[0] == "env" or verb.startswith("repos ") or verb == "workspace get-status":
            return self._git(verb, a)
        if verb == "current-user me" and a[a.index("--profile") + 1] == "weather-signal-agent":
            return self._answer("agent me", a)
        return self._answer(verb, a)

    def _genie_write(self, verb: str, a: list[str]) -> Result:
        body = json.loads(Path(a[a.index("--json") + 1].removeprefix("@")).read_text())
        if verb == "genie update-space":
            self.updated.append((a[3], body))
            for space in self.spaces + self.pending:
                if space["space_id"] == a[3]:
                    space.update(body)
            return ok({"space_id": a[3]})
        self.created.append(body)
        sid = f"g-{len(self.spaces) + 1}"
        self.pending.append(
            {"space_id": sid, **{k: body[k] for k in ("title", "description", "serialized_space")}}
        )
        return ok({"space_id": sid})

    def _git(self, verb: str, a: list[str]) -> Result:
        """git ls-remote, the path's status and the Repos API, over `git_folder`."""
        f = self.git_folder
        if a[0] == "env":
            ok_ = self.remote_head is not None
            return Result(0, f"{self.remote_head}\t{a[-1]}\n", "") if ok_ else Result(128, "", "")
        if verb == "workspace get-status":
            if f is None:
                return Result(1, "", f"Error: Path ({a[3]}) doesn't exist.\n")
            return ok({"object_type": f["object_type"], "object_id": f.get("id", 5), "path": a[3]})
        if verb == "repos create":
            self.git_folder = f = {"object_type": "REPO", "id": 78, "url": a[3], "branch": "dev"}
            f["head_commit_id"] = "old"
            return ok({"id": 78, "url": a[3], "branch": "dev"})
        if verb == "repos update" and f is not None:
            f["branch"], f["head_commit_id"] = a[a.index("--branch") + 1], self.remote_head
        return ok({k: v for k, v in (f or {}).items() if k != "object_type"})

    def _jobs(self, name: str) -> Result:
        st = self.jobs.get(name)
        job = {"job_id": self._id(name), "settings": {"schedule": {"pause_status": st}}}
        return ok([job] if st else [])

    def _launchctl(self, a: list[str]) -> Result:
        if a[0] == "pgrep":
            self.straggler -= 1
            return Result(0, "4242\n", "") if self.straggler >= 0 else Result(1, "", "")
        if a[1] == "bootout":
            self.launched = False
            return Result(0, "", "")
        if a[2].count("/") == 1:  # the domain itself
            return Result(0, "", "") if self.gui else Result(112, "", "Could not find domain")
        if not self.gui:
            return Result(112, "", "Could not find domain")
        state = "running" if self.agent_running else "not running"
        return Result(0, f"\tstate = {state}\n", "") if self.launched else Result(113, "", "")

    def _id(self, name: str) -> int:
        return sorted(self.jobs).index(name) + 1 if name in self.jobs else 0

    def _update(self, a: list[str]) -> Result:
        """`jobs update`: the schedule's new pause status lands on the job with that id."""
        body = json.loads(a[a.index("--json") + 1])
        (name,) = [n for n in self.jobs if self._id(n) == body["job_id"]]
        self.jobs[name] = body["new_settings"]["schedule"]["pause_status"]
        return ok({})

    def _answer(self, verb: str, a: list[str]) -> Result:
        if verb == "version":
            return Result(0, "Databricks CLI v1.26.0\n", "")
        answers: dict[str, Any] = {
            "current-user me": {"userName": "owner@example.com"},
            "agent me": {"userName": "a-1"},
            "metastores current": {"default_catalog_name": "workspace"},
            "service-principal-secrets-proxy create": {"id": "s-1", "secret": FAKE_SECRET},
            "catalogs get": {"name": a[3]},
            "warehouses list": self.warehouses,
            "schemas list": [{"name": n} for n in self.schemas],
            "service-principals list": self.principals,
            "secrets list-secrets": self.secrets,
            "bundle plan": self.plan,
            "git-credentials list": self.git_credentials,
        }
        if verb == "genie list-spaces":
            # A new space lists after `list_lag` more list calls, as the API does after a create.
            if self.pending and self.list_lag <= 0:
                self.spaces += self.pending
                self.pending = []
            self.list_lag -= 1
            return ok({"spaces": self.spaces})
        if verb in answers:
            return ok(answers[verb])
        if verb in ("genie create-space", "genie update-space"):
            return self._genie_write(verb, a)
        if verb == "tables list":
            schema = a[4].removeprefix("dev_")
            return ok([{"name": t} for t in self.tables.get(schema, [])])
        if verb == "apps get":
            return ok(self.app) if self.app else Result(1, "", "Error: App does not exist.\n")
        if verb in ("workspace list", "workspace delete") and self.artifacts is not None:
            return self._artifacts(verb, a)
        if verb == "workspace export":
            return (
                Result(0, self.metadata, "")
                if self.metadata
                else Result(1, "", f"Error: Path ({a[3]}) doesn't exist.\n")
            )
        return Result(0, "{}", "")

    def _artifacts(self, verb: str, a: list[str]) -> Result:
        """`workspace list` of a folder's direct children, and a recursive `workspace delete`."""
        held = self.artifacts or {}
        if verb == "workspace delete":
            self.artifacts = {k: v for k, v in held.items() if not k.startswith(a[3] + "/")}
            self.artifacts.pop(a[3], None)
            return ok({})
        bare = a[3].removeprefix("/Workspace")  # the API may answer without that prefix
        kids = [{"path": k, **v} for k, v in held.items()
                if k.rsplit("/", 1)[0].removeprefix("/Workspace") == bare]  # fmt: skip
        return ok(kids) if kids else Result(1, "", f"Error: Path ({a[3]}) doesn't exist.\n")


def agent_installed(c: Ctx) -> None:
    """A Mac where the agent's profile holds a secret and its LaunchAgent is written."""
    c.cfg_file.write_text(
        f"[p]\nhost = {HOST}\n\n[weather-signal-agent]\nhost = {HOST}\nclient_id = a-1\n"
        "client_secret = already-there\n",
        encoding="utf-8",
    )
    path = launchd.plist_path(c.home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        launchd.plist(
            python=c.python, profile="weather-signal-agent", databricks_dir="/bin",
            home=c.home, prefix=c.scope.prefix,
        )
    )  # fmt: skip


def app_deployed(c: Ctx) -> None:
    """The App's last good deployment on this machine was of this tree's app/."""
    c.state_dir.mkdir(parents=True, exist_ok=True)
    stamp = c.state_dir / f"app-{c.scope.target}-{c.scope.prefix}.digest"
    stamp.write_text(wheel.digest(c.repo, wheel.APP) + "\n", encoding="utf-8")


@pytest.fixture
def ws() -> Workspace:
    return Workspace()


@pytest.fixture
def ctx(ws: Workspace, tmp_path: Path) -> Ctx:
    key = tmp_path / "API_KEY"
    key.write_text("k-123\n", encoding="utf-8")
    archive = tmp_path / "data"
    (archive / "raw").mkdir(parents=True)
    (archive / "manifest.json").write_text('{"files": []}', encoding="utf-8")
    store = FakeStore()
    store.files["/Volumes/workspace/dev_signals/ui/snapshot.json"] = b"{}"
    c = Ctx(
        scope=scope.build("p", "dev", None, "workspace"),
        run=ws,
        sql=lambda q: [{"n": 3369}],
        store=store,
        list_volume=lambda root: {},
        setup=lambda *a: c.setups.append(a),  # type: ignore[attr-defined]
        champion_tags=lambda s: {"fit_as_of": "2026-01-02T21:30:00Z", "fit": phases.TRAIN_FIT},
        now=lambda: datetime(2026, 11, 18, 12, 0, tzinfo=UTC),
        sleep=lambda s: None,
        say=lambda line: c.lines.append(line),  # type: ignore[attr-defined]
        key_file=key,
        repo=REPO,
        seed_from=archive,
        importable=lambda m: True,
    )
    c.home, c.cfg_file, c.python = tmp_path / "home", tmp_path / "databrickscfg", "/venv/bin/python"
    c.platform = "darwin"
    c.state_dir, c.linked_main = tmp_path / "state", lambda repo: None
    app_deployed(c)
    agent_installed(c)
    c.setups = []  # type: ignore[attr-defined]
    c.lines = []  # type: ignore[attr-defined]
    return c


from tests.unit.deploy.proof_world import p  # noqa: E402, F401  (the proof's fixture)
