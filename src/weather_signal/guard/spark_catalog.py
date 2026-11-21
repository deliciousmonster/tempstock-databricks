"""The guard's catalog over SQL, through a Spark session: a serverless job's or Databricks Connect's."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from weather_signal.catalog.ddl import TableSpec
from weather_signal.config import Names
from weather_signal.guard.catalog import (
    GuardRow,
    Incident,
    NotFound,
    RestoreFailed,
    Row,
    Unreadable,
)
from weather_signal.guard.rules import Act, Commit, PlannedCommit

NOT_FOUND = ("TABLE_OR_VIEW_NOT_FOUND", "DELTA_TABLE_NOT_FOUND", "DELTA_MISSING_DELTA_TABLE")
RESTORE_FAILED = ("DELTA_FILE_NOT_FOUND", "FileNotFound", "DELTA_CANNOT_RESTORE")


def _lit(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(v)
    if isinstance(v, datetime):
        return f"TIMESTAMP'{v.isoformat()}'"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _aware(v: Any) -> Any:
    """PySpark collects timestamps as naive local time; Python reads naive as local, so this is exact."""
    return v.astimezone(UTC) if isinstance(v, datetime) else v


def _plain(r: Any) -> Row:
    return {
        k: (_aware(v).isoformat() if isinstance(v, datetime) else v)
        for k, v in r.asDict(True).items()
    }


class SparkCatalog:
    def __init__(self, spark: Any, n: Names) -> None:
        self.spark, self.ops = spark, n.schema("ops")

    def _sql(self, q: str) -> list[Any]:
        try:
            return list(self.spark.sql(q).collect())
        except Exception as err:
            text = str(err)
            if any(code in text for code in NOT_FOUND):
                raise NotFound(text[:200]) from err
            raise Unreadable(text[:200]) from err

    def current_user(self) -> str:
        return str(self._sql("SELECT current_user() AS u")[0]["u"])

    def table_id(self, table: str) -> str:
        return str(self._sql(f"DESCRIBE DETAIL {table}")[0]["id"])

    def latest_version(self, table: str) -> int:
        return int(self._sql(f"DESCRIBE HISTORY {table} LIMIT 1")[0]["version"])

    def properties(self, table: str) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self._sql(f"SHOW TBLPROPERTIES {table}")}

    def columns(self, table: str) -> list[str]:
        return [str(r["col_name"]) for r in self._sql(f"SHOW COLUMNS IN {table}")]

    def filters_and_masks(self, table: str) -> int:
        cat, schema, name = table.split(".")
        where = f"table_schema = {_lit(schema)} AND table_name = {_lit(name)}"
        n = 0
        for view in ("row_filters", "column_masks"):
            n += int(
                self._sql(
                    f"SELECT count(*) AS n FROM {cat}.information_schema.{view} WHERE {where}"
                )[0]["n"]
            )
        return n

    def history(self, table: str, after: int) -> list[Commit]:
        rows = self._sql(
            f"SELECT version, operation, operationParameters, userName, timestamp "
            f"FROM (DESCRIBE HISTORY {table}) WHERE version > {int(after)} ORDER BY version"
        )
        return [
            Commit(
                int(r["version"]),
                r["operation"],
                dict(r["operationParameters"] or {}),
                r["userName"] or "",
                _aware(r["timestamp"]),
            )
            for r in rows
        ]

    def version_diff_empty(self, table: str, version: int) -> bool | None:
        a, b = f"{table} VERSION AS OF {version}", f"{table} VERSION AS OF {version - 1}"
        try:
            n = self._sql(
                f"SELECT (SELECT count(*) FROM (SELECT * FROM {a} EXCEPT ALL SELECT * FROM {b})) + "
                f"(SELECT count(*) FROM (SELECT * FROM {b} EXCEPT ALL SELECT * FROM {a})) AS n"
            )
        except Unreadable:
            return None
        return int(n[0]["n"]) == 0

    def diff_since(self, table: str, good: int) -> tuple[list[Row], list[Row]]:
        g = f"{table} VERSION AS OF {good}"
        removed = self._sql(f"SELECT * FROM {g} EXCEPT ALL SELECT * FROM {table}")
        added = self._sql(f"SELECT * FROM {table} EXCEPT ALL SELECT * FROM {g}")
        return [_plain(r) for r in removed], [_plain(r) for r in added]

    def table_changes(self, table: str, first: int, last: int) -> list[Row] | None:
        try:
            return [
                _plain(r)
                for r in self._sql(f"SELECT * FROM table_changes('{table}', {first}, {last})")
            ]
        except (Unreadable, NotFound):
            return None

    def rows(self, table: str, version: int | None = None) -> list[Row]:
        src = table if version is None else f"{table} VERSION AS OF {version}"
        return [_plain(r) for r in self._sql(f"SELECT * FROM {src}")]

    def valid_new_rows(self, table: str, source: str, good: int | None, spec: TableSpec):
        base = table if good is None else f"{table} VERSION AS OF {good}"
        on = " AND ".join(f"s.{k} <=> b.{k}" for k in spec.key)
        valid = " AND ".join(f"coalesce(({e}), false)" for e in spec.constraints.values()) or "true"
        new = f"SELECT s.* FROM {source} s LEFT ANTI JOIN {base} b ON {on}"
        changed = (
            f"SELECT s.* FROM {source} s LEFT SEMI JOIN {base} b ON {on} "
            f"EXCEPT ALL SELECT * FROM {base}"
        )
        kept = self._sql(f"SELECT * FROM ({new}) WHERE {valid}")
        dropped = self._sql(f"SELECT * FROM ({new}) WHERE NOT ({valid}) UNION ALL {changed}")
        return [_plain(r) for r in kept], [_plain(r) for r in dropped]

    def execute(self, sql: str) -> None:
        self._sql(sql)

    def restore(self, table: str, version: int) -> None:
        try:
            self._sql(f"RESTORE TABLE {table} TO VERSION AS OF {int(version)}")
        except Unreadable as err:
            if any(code in str(err) for code in RESTORE_FAILED):
                raise RestoreFailed(str(err)) from err
            raise

    def insert(self, table: str, rows: list[Row]) -> None:
        """Rows come back as `_plain` made them (ISO strings); JSON parses them to the table's types."""
        if rows:
            from pyspark.sql import functions as F

            schema = self.spark.table(table).schema
            js = self.spark.createDataFrame(
                [(json.dumps(r, default=str),) for r in rows], "j string"
            )
            js.select(F.from_json("j", schema).alias("r")).select("r.*").writeTo(table).append()

    def rename(self, table: str, new_name: str) -> None:
        self._sql(f"ALTER TABLE {table} RENAME TO {new_name}")

    def undrop(self, table: str) -> None:
        self._sql(f"UNDROP TABLE {table}")

    # ops ---------------------------------------------------------------------------------------
    def guard_rows(self) -> list[GuardRow]:
        cols = [f for f in GuardRow.__dataclass_fields__]
        rows = self._sql(f"SELECT {', '.join(cols)} FROM {self.ops}.guard_state")
        return [GuardRow(**{c: _aware(r[c]) for c in cols}) for r in rows]

    def put_guard_row(self, row: GuardRow) -> None:
        d = asdict(row)
        sel = ", ".join(f"{_lit(v)} AS {k}" for k, v in d.items())
        sets = ", ".join(f"t.{k} = s.{k}" for k in d)
        self._sql(
            f"MERGE INTO {self.ops}.guard_state t USING (SELECT {sel}) s "
            f"ON t.table_name = s.table_name WHEN MATCHED THEN UPDATE SET {sets} "
            "WHEN NOT MATCHED THEN INSERT *"
        )

    def incidents(self, table_id: str) -> list[Incident]:
        rows = self._sql(
            f"SELECT * FROM {self.ops}.guard_incidents WHERE table_id = {_lit(table_id)}"
        )
        return [
            Incident(
                r["table_name"],
                r["table_id"],
                r["first_bad_version"],
                r["last_bad_version"],
                r["kind"],
                [Commit(**c) for c in json.loads(r["commits"] or "[]")],
                json.loads(r["removed_since_good"] or "[]"),
                json.loads(r["added_since_good"] or "[]"),
                json.loads(r["table_changes"]) if r["table_changes"] else None,
                _aware(r["recorded_at"]),
                json.loads(r["dropped"]) if r["dropped"] else None,
            )
            for r in rows
        ]

    def _incident_values(self, i: Incident) -> dict[str, Any]:
        def js(v: Any) -> str | None:
            return None if v is None else json.dumps(v, default=str)

        return {
            "table_name": i.table_name,
            "table_id": i.table_id,
            "first_bad_version": i.first_bad_version,
            "last_bad_version": i.last_bad_version,
            "kind": i.kind,
            "commits": js([asdict(c) for c in i.commits]),
            "removed_since_good": js(i.removed_since_good),
            "added_since_good": js(i.added_since_good),
            "table_changes": js(i.table_changes),
            "dropped": js(i.dropped),
            "recorded_at": i.recorded_at,
        }

    def add_incident(self, incident: Incident) -> None:
        d = self._incident_values(incident)
        self._sql(
            f"INSERT INTO {self.ops}.guard_incidents ({', '.join(d)}) "
            f"VALUES ({', '.join(_lit(v) for v in d.values())})"
        )

    def update_incident(self, incident: Incident) -> None:
        d = self._incident_values(incident)
        self._sql(
            f"UPDATE {self.ops}.guard_incidents SET dropped = {_lit(d['dropped'])} "
            f"WHERE table_id = {_lit(incident.table_id)} "
            f"AND first_bad_version = {incident.first_bad_version}"
        )

    def acts(self, table: str) -> list[Act]:
        rows = self._sql(f"SELECT * FROM {self.ops}.guard_acts WHERE table_name = {_lit(table)}")
        return [
            Act(
                r["act_id"],
                r["table_id"],
                int(r["read_version"]),
                r["user_name"],
                [PlannedCommit(**p) for p in json.loads(r["planned"])],
                r["status"],
                json.loads(r["observed"] or "[]"),
            )
            for r in rows
        ]

    def put_act(self, table: str, act: Act) -> None:
        d = {
            "table_name": table,
            "act_id": act.act_id,
            "table_id": act.table_id,
            "read_version": act.read_version,
            "user_name": act.user,
            "planned": json.dumps([asdict(p) for p in act.planned]),
            "status": act.status,
            "observed": json.dumps(act.observed),
        }
        sel = ", ".join(f"{_lit(v)} AS {k}" for k, v in d.items())
        self._sql(
            f"MERGE INTO {self.ops}.guard_acts t USING (SELECT {sel}) s ON t.act_id = s.act_id "
            "WHEN MATCHED THEN UPDATE SET t.table_id = s.table_id, t.status = s.status, "
            "t.observed = s.observed WHEN NOT MATCHED THEN INSERT *"
        )
