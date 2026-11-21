"""What every phase reads and calls, passed in so each phase can be driven by fakes."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from weather_signal import config
from weather_signal.deploy import wheel
from weather_signal.deploy.checkout import linked_main
from weather_signal.deploy.cli import Runner, Stop, Unknown, call_json, not_found
from weather_signal.deploy.scope import Scope
from weather_signal.deploy.seed import Entry, ListVolume
from weather_signal.ingest.files import ReadStore

Sql = Callable[[str], list[dict[str, Any]]]
Setup = Callable[[Scope, str, str, str], None]
Tags = Callable[[Scope], Mapping[str, str] | None]


def _importable(module: str) -> bool:
    """`find_spec` of a dotted name raises when its parent package is absent."""
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False


@dataclass
class Ctx:
    scope: Scope
    run: Runner
    sql: Sql
    store: ReadStore
    list_volume: ListVolume
    setup: Setup
    champion_tags: Tags
    now: Callable[[], datetime]
    sleep: Callable[[float], None]
    say: Callable[[str], None]
    key_file: Path
    repo: Path
    seed_from: Path | None = None
    ledger_from: Path | None = None  # a proof's archived ledger, restored before catch-up
    skip_app: bool = False
    warehouse_name: str | None = None
    needs_connect: bool = True  # false when no phase ahead opens a Spark session
    importable: Callable[[str], bool] = _importable
    live: bool = False  # --live: prod's schedules and agent go live in the last phases
    discover_catalog: bool = False  # no --catalog: preflight reads the metastore's default
    argv: tuple[str, ...] = ()  # bootstrap's own arguments, for the one command preflight prints
    python: str = sys.executable
    home: Path = field(default_factory=Path.home)
    cfg_file: Path = field(
        default_factory=lambda: Path(
            os.environ.get("DATABRICKS_CONFIG_FILE", Path.home() / ".databrickscfg")
        )
    )
    wheel_tag: str = ""  # wheel.tag(repo), read on the first bundle command
    pause: str = "PAUSED"  # every schedule's status in a deploy; the live phase sets UNPAUSED
    was_live: bool | None = None  # prod's deployed schedules, read once before any deploy
    platform: str = sys.platform
    git_folder_url: str = config.GIT_FOLDER_URL
    git_folder_branch: str = config.GIT_FOLDER_BRANCH
    git_folder_skip: str = ""  # set when a Repos write found the GitHub link without access
    # Bootstrap's own local records: the lock, a held live intent, a just-created Genie space.
    linked_main: Callable[[Path], Path | None] = linked_main
    state_dir: Path = field(default_factory=lambda: Path.home() / ".local/state/weather-signal")
    # Filled by preflight and principals, read by the phases after them.
    warehouse_id: str = ""
    principals: dict[str, str] = field(default_factory=dict)
    seed: list[Entry] = field(default_factory=list)
    seed_skipped: Counter[str] = field(default_factory=Counter)
    seed_kept: set[str] = field(default_factory=set)  # landed copies of the same request, kept

    def goes_live(self) -> bool:
        """dev always (the owner's "start it"); prod only with --live; scratch never."""
        return self.scope.target == "dev" or (self.scope.target == "prod" and self.live)

    def cli(self, *args: str) -> Any:
        return call_json(self.run, ["databricks", *args, "--profile", self.scope.profile])

    def bundle(self, verb: str, *args: str) -> list[str]:
        self.wheel_tag = self.wheel_tag or wheel.tag(self.repo)
        return [
            "databricks", "bundle", verb, *args,
            *self.scope.bundle_args(self.warehouse_id, self.pause),
            "--var", f"wheel_tag={self.wheel_tag}",
        ]  # fmt: skip

    def bundle_run(self, key: str, *extra: str) -> None:
        r = self.run([*self.bundle("run", key), *extra])
        if r.rc != 0:
            raise Stop(1, f"bundle run {key} failed (exit {r.rc}):\n{r.tail()}")

    def app(self) -> dict[str, Any] | None:
        argv = ["databricks", "apps", "get", self.scope.app_name, "--profile", self.scope.profile]
        r = self.run([*argv, "-o", "json"])
        if not_found(r):
            return None
        if r.rc != 0:
            raise Unknown(f"apps get {self.scope.app_name}: exit {r.rc}: {r.tail(3)}")
        try:
            doc: dict[str, Any] = json.loads(r.out)
        except json.JSONDecodeError as err:
            raise Unknown(f"apps get {self.scope.app_name}: output is not JSON") from err
        return doc
