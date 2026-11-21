"""What the guard reads and writes, as a protocol; `SparkCatalog` and the tests' fake implement it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from weather_signal.catalog.ddl import TableSpec
from weather_signal.guard.rules import Act, Commit

Row = dict[str, Any]


class NotFound(Exception):
    """TABLE_OR_VIEW_NOT_FOUND on the guarded name."""


class Unreadable(Exception):
    """Any other read failure: the guard's unknown."""


class RestoreFailed(Exception):
    """RESTORE could not run, as when the files it needs were vacuumed."""


@dataclass
class GuardRow:
    table_name: str
    table_id: str
    checked_version: int
    open_incident: bool = False
    first_bad_version: int | None = None
    last_bad_version: int | None = None
    pre_verified_as_of: datetime | None = None
    post_verified_as_of: datetime | None = None
    last_notified_at: datetime | None = None
    updated_by: str | None = None


@dataclass
class Incident:
    table_name: str
    table_id: str
    first_bad_version: int
    last_bad_version: int
    kind: str
    commits: list[Commit]
    removed_since_good: list[Row]
    added_since_good: list[Row]
    table_changes: list[Row] | None
    recorded_at: datetime
    dropped: list[Row] | None = None


class Catalog(Protocol):
    def current_user(self) -> str: ...

    # The guarded table
    def table_id(self, table: str) -> str: ...
    def latest_version(self, table: str) -> int: ...
    def properties(self, table: str) -> dict[str, str]: ...
    def columns(self, table: str) -> list[str]: ...
    def filters_and_masks(self, table: str) -> int: ...
    def history(self, table: str, after: int) -> list[Commit]: ...
    def version_diff_empty(self, table: str, version: int) -> bool | None: ...
    def diff_since(self, table: str, good: int) -> tuple[list[Row], list[Row]]: ...
    def table_changes(self, table: str, first: int, last: int) -> list[Row] | None: ...
    def rows(self, table: str, version: int | None = None) -> list[Row]: ...
    def valid_new_rows(
        self, table: str, source: str, good: int | None, spec: TableSpec
    ) -> tuple[list[Row], list[Row]]: ...

    # Acts on the guarded table
    def execute(self, sql: str) -> None: ...
    def restore(self, table: str, version: int) -> None: ...
    def insert(self, table: str, rows: list[Row]) -> None: ...
    def rename(self, table: str, new_name: str) -> None: ...
    def undrop(self, table: str) -> None: ...

    # ops
    def guard_rows(self) -> list[GuardRow]: ...
    def put_guard_row(self, row: GuardRow) -> None: ...
    def incidents(self, table_id: str) -> list[Incident]: ...
    def add_incident(self, incident: Incident) -> None: ...
    def update_incident(self, incident: Incident) -> None: ...
    def acts(self, table: str) -> list[Act]: ...
    def put_act(self, table: str, act: Act) -> None: ...
