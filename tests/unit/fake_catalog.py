"""An in-memory catalog with Delta-like versions, snapshots and history, for the guard and setup."""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any

from weather_signal.catalog.ddl import LLM_ROUTES, TableSpec
from weather_signal.guard.catalog import GuardRow, Incident, NotFound, RestoreFailed, Unreadable
from weather_signal.guard.rules import Act, Commit

Row = dict[str, Any]
CREATE = re.compile(
    r"CREATE TABLE IF NOT EXISTS (\S+) \((.*?)\)(?: CLUSTER BY \(.*?\))?(?: TBLPROPERTIES \((.*)\))?$"
)
CHECKS = {
    "action_valid": lambda r: r.get("action") in ("buy", "sell", "hold"),
    "confidence_range": lambda r: r.get("confidence") is not None and 0 <= r["confidence"] <= 1,
    "never_flip": lambda r: r.get("action") in (r.get("signal_action"), "hold"),
    "action_not_null": lambda r: r.get("action") is not None,
    "confidence_not_null": lambda r: r.get("confidence") is not None,
    "llm_route_valid": lambda r: r.get("llm_route") in (None, *LLM_ROUTES),
}


@dataclass
class Version:
    commit: Commit
    rows: list[Row]
    props: dict[str, str]


@dataclass
class Table:
    id: str
    versions: list[Version] = field(default_factory=list)
    filters: int = 0
    columns: list[str] = field(default_factory=list)

    @property
    def rows(self) -> list[Row]:
        return self.versions[-1].rows

    @property
    def props(self) -> dict[str, str]:
        return self.versions[-1].props


def _key(r: Row) -> tuple:
    return tuple(sorted((k, repr(v)) for k, v in r.items()))


class FakeCatalog:
    def __init__(self, user: str = "owner@example.com") -> None:
        self.user = user
        self.tables: dict[str, Table] = {}
        self.dropped: dict[str, Table] = {}
        self.guard: dict[str, GuardRow] = {}
        self.incident_rows: list[Incident] = []
        self.act_rows: dict[str, list[Act]] = {}
        self.log: list[str] = []
        self.unreadable: set[str] = set()
        self.missing: set[str] = set()
        self.restore_fails = False

    # helpers for tests ------------------------------------------------------------------------
    def commit(
        self,
        name: str,
        op: str,
        params: dict | None = None,
        *,
        rows: list[Row] | None = None,
        props: dict[str, str] | None = None,
        user: str | None = None,
    ) -> int:
        t = self.tables[name]
        last = t.versions[-1] if t.versions else Version(Commit(-1, "", {}, ""), [], {})
        v = len(t.versions)
        t.versions.append(
            Version(
                Commit(v, op, params or {}, user or self.user),
                list(last.rows if rows is None else rows),
                dict(last.props if props is None else props),
            )
        )
        self.log.append(f"{op} {name}@{v}")
        return v

    def create(self, name: str, props: dict[str, str] | None = None) -> None:
        self.tables[name] = Table(uuid.uuid4().hex)
        self.commit(name, "CREATE TABLE", {}, rows=[], props=props or {})

    # Catalog ----------------------------------------------------------------------------------
    def current_user(self) -> str:
        return self.user

    def _t(self, name: str) -> Table:
        if name in self.unreadable:
            raise Unreadable(name)
        if name not in self.tables:
            raise NotFound(name)
        return self.tables[name]

    def table_id(self, table: str) -> str:
        return self._t(table).id

    def latest_version(self, table: str) -> int:
        return len(self._t(table).versions) - 1

    def properties(self, table: str) -> dict[str, str]:
        return dict(self._t(table).props)

    def columns(self, table: str) -> list[str]:
        return list(self._t(table).columns)

    def filters_and_masks(self, table: str) -> int:
        return self._t(table).filters

    def history(self, table: str, after: int) -> list[Commit]:
        return [v.commit for v in self._t(table).versions if v.commit.version > after]

    def version_diff_empty(self, table: str, version: int) -> bool | None:
        vs = self._t(table).versions
        return Counter(map(_key, vs[version].rows)) == Counter(map(_key, vs[version - 1].rows))

    def rows(self, table: str, version: int | None = None) -> list[Row]:
        t = self._t(table)
        return list((t.versions[version] if version is not None else t.versions[-1]).rows)

    def diff_since(self, table: str, good: int) -> tuple[list[Row], list[Row]]:
        then, now = Counter(map(_key, self.rows(table, good))), Counter(map(_key, self.rows(table)))
        by = {_key(r): r for r in self.rows(table, good) + self.rows(table)}
        return ([by[k] for k in (then - now).elements()], [by[k] for k in (now - then).elements()])

    def table_changes(self, table: str, first: int, last: int) -> list[Row] | None:
        if self._t(table).props.get("delta.enableChangeDataFeed") != "true":
            return None
        return [{"version": v} for v in range(first, last + 1)]

    def valid_new_rows(self, table: str, source: str, good: int | None, spec: TableSpec):
        base = self.rows(table, good) if good is not None else self.rows(table)
        known = {tuple(r[k] for k in spec.key): r for r in base}
        kept, dropped = [], []
        for r in self.rows(source):
            k = tuple(r[c] for c in spec.key)
            if k in known:
                if _key(known[k]) != _key(r):
                    dropped.append(r)
                continue
            (kept if all(CHECKS[n](r) for n in spec.constraints) else dropped).append(r)
        return kept, dropped

    def execute(self, sql: str) -> None:
        self.log.append(sql)
        if m := CREATE.match(sql):
            if m.group(1) not in self.tables:
                self.create(
                    m.group(1), dict(re.findall(r"'([^']+)' = '([^']+)'", m.group(3) or ""))
                )
                self.tables[m.group(1)].columns = [c.split()[0] for c in m.group(2).split(", ")]
        elif m := re.match(r"ALTER TABLE (\S+) ADD COLUMNS \((.*)\)$", sql):
            self.tables[m.group(1)].columns += [c.split()[0] for c in m.group(2).split(", ")]
            self.commit(m.group(1), "ADD COLUMNS", {"columns": m.group(2)})
        elif m := re.match(r"ALTER TABLE (\S+) ADD CONSTRAINT (\w+) CHECK \((.*)\)$", sql):
            name, cname, expr = m.groups()
            props = {**self.tables[name].props, f"delta.constraints.{cname}": expr}
            self.commit(name, "ADD CONSTRAINT", {"name": cname, "expr": expr}, props=props)
        elif m := re.match(r"ALTER TABLE (\S+) DROP CONSTRAINT (\w+)$", sql):
            name, cname = m.groups()
            props = {
                k: v
                for k, v in self.tables[name].props.items()
                if k != f"delta.constraints.{cname}"
            }
            self.commit(name, "DROP CONSTRAINT", {"constraint": cname}, props=props)
        elif m := re.match(r"ALTER TABLE (\S+) SET TBLPROPERTIES \((.*)\)$", sql):
            new = dict(re.findall(r"'([^']+)' = '([^']+)'", m.group(2)))
            self.commit(
                m.group(1),
                "SET TBLPROPERTIES",
                {"properties": json.dumps(new, separators=(",", ":"))},
                props={**self.tables[m.group(1)].props, **new},
            )

    def restore(self, table: str, version: int) -> None:
        if self.restore_fails:
            raise RestoreFailed("DELTA_FILE_NOT_FOUND")
        v = self._t(table).versions[version]
        self.commit(table, "RESTORE", {"version": str(version)}, rows=v.rows, props=v.props)

    def insert(self, table: str, rows: list[Row]) -> None:
        self.commit(table, "WRITE", {"mode": "Append"}, rows=self.rows(table) + list(rows))

    def rename(self, table: str, new_name: str) -> None:
        self.tables[new_name] = self._t(table)
        del self.tables[table]
        self.log.append(f"RENAME {table} {new_name}")

    def undrop(self, table: str) -> None:
        """Like UNDROP: the most recent drop under the name, or an error that is not NotFound."""
        if table not in self.dropped:
            raise Unreadable(f"UNDROP_TABLE_NOT_FOUND {table}")
        self.tables[table] = self.dropped.pop(table)
        self.log.append(f"UNDROP {table}")

    def drop(self, table: str) -> None:
        self.dropped[table] = self.tables.pop(table)

    # ops --------------------------------------------------------------------------------------
    def guard_rows(self) -> list[GuardRow]:
        if "ops.guard_state" in self.unreadable:
            raise Unreadable("ops.guard_state")
        if "ops.guard_state" in self.missing:
            raise NotFound("ops.guard_state")
        return [replace(r) for r in self.guard.values()]

    def put_guard_row(self, row: GuardRow) -> None:
        self.guard[row.table_name] = replace(row)

    def incidents(self, table_id: str) -> list[Incident]:
        return [i for i in self.incident_rows if i.table_id == table_id]

    def add_incident(self, incident: Incident) -> None:
        self.incident_rows.append(incident)

    def update_incident(self, incident: Incident) -> None:
        for i, row in enumerate(self.incident_rows):
            if (row.table_id, row.first_bad_version) == (
                incident.table_id,
                incident.first_bad_version,
            ):
                self.incident_rows[i] = incident

    def acts(self, table: str) -> list[Act]:
        return [replace(a, observed=list(a.observed)) for a in self.act_rows.get(table, [])]

    def put_act(self, table: str, act: Act) -> None:
        rows = self.act_rows.setdefault(table, [])
        rows[:] = [a for a in rows if a.act_id != act.act_id] + [
            replace(act, observed=list(act.observed))
        ]
        self.log.append(f"ACT {act.status} {table}")
