"""Where the worker reads inputs and writes rows: Unity Catalog over Databricks Connect, or a local fixture."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

from weather_signal.agent import runlog
from weather_signal.agent.merge import merge_sql
from weather_signal.config import PRIMARY, Names

log = logging.getLogger(__name__)

PRICE_DAYS = 30
INPUT_KEYS = (
    "signal_action",
    "score",
    "signal_confidence",
    "reasons",
    "horizon_days",
    "inputs_max_known_at",
    "forecast_known_at",
    "last_close",
    "ret_5d",
)
# What the guard says of one as_of; only GUARD_OK lets an hour run.
GUARD_OK, GUARD_UNVERIFIED, GUARD_INCIDENT = "ok", "unverified", "incident"


class Store(Protocol):
    def inputs(self, as_of: datetime) -> dict[str, dict[str, Any]]: ...
    def prices(self) -> list[dict[str, Any]]: ...
    def guard(self, as_of: datetime) -> str: ...
    def writer(self) -> str: ...
    def merge(self, binds: list[dict[str, Any]], *, llm_route: str) -> int: ...
    def run_ids_present(self, ids: Iterable[str]) -> set[str]: ...
    def insert_run(self, row: dict[str, Any]) -> None: ...


def join(
    agent_rows: list[dict[str, Any]], forecast_rows: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Each date's `fn_agent_inputs` row with the latest-known `fn_forecast_index` row for that date."""
    latest: dict[str, dict[str, Any]] = {}
    for f in forecast_rows:
        d = str(f["target_date"])
        if d not in latest or str(f["known_at"]) > str(latest[d]["known_at"]):
            latest[d] = f
    out = {}
    for r in agent_rows:
        d = str(r["target_date"])
        row = {k: _plain(r.get(k)) for k in INPUT_KEYS}
        f = latest.get(d, {})
        row["index_c"] = _plain(f.get("index_c"))
        row["normal_c"] = _plain(f.get("normal_c"))
        row["anomaly_c"] = _plain(f.get("anomaly_c", r.get("forecast_anomaly_c")))
        out[d] = row
    return out


def _instant(v: datetime) -> datetime:
    """A collected TIMESTAMP as aware UTC; a naive one is in the process zone, as Connect made it."""
    return v.astimezone(UTC)


def _plain(v: Any) -> Any:
    if isinstance(v, datetime):
        return _instant(v).isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, list | tuple):
        return [_plain(x) for x in v]
    return v


def project(row: dict[str, Any], columns: Iterable[str]) -> dict[str, Any]:
    """`row` on the table's columns; anything the table lacks goes into `detail` as JSON."""
    cols = set(columns)
    out = {k: v for k, v in row.items() if k in cols}
    rest = {k: v for k, v in row.items() if k not in cols}
    if rest and "detail" in cols:
        out["detail"] = json.dumps(rest, sort_keys=True, default=str)
    return out


class SparkStore:
    def __init__(self, spark: Any, names: Names, *, eval_run: str | None = None):
        self.spark = spark
        self.n = names
        self.eval_run = eval_run
        self.signals = names.schema("signals")

    def _rows(self, q: str, args: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return [r.asDict() for r in self.spark.sql(q, args=args or {}).collect()]

    def inputs(self, as_of: datetime) -> dict[str, dict[str, Any]]:
        a = self._rows(f"SELECT * FROM {self.signals}.fn_agent_inputs(:as_of)", {"as_of": as_of})
        if not a:
            return {}
        f = self._rows(f"SELECT * FROM {self.signals}.fn_forecast_index(:as_of)", {"as_of": as_of})
        return join(a, f)

    def prices(self) -> list[dict[str, Any]]:
        q = f"SELECT * FROM {self.signals}.fn_prices(:t, :d) ORDER BY trade_date"
        rows = self._rows(q, {"t": PRIMARY, "d": PRICE_DAYS})
        return [{**{k: _plain(v) for k, v in r.items()}, "ticker": PRIMARY} for r in rows]

    def guard(self, as_of: datetime) -> str:
        """An incident unless `open_incident` is false; then ok only once this as_of is post-verified."""
        rows = self._rows(f"SELECT * FROM {self.signals}.v_guard_hold")
        if not rows or rows[0]["open_incident"] is not False:
            return GUARD_INCIDENT
        verified = rows[0]["post_verified_as_of"]
        if verified is not None and _instant(verified) == as_of.astimezone(UTC):
            return GUARD_OK
        return GUARD_UNVERIFIED

    def writer(self) -> str:
        return str(self._rows("SELECT current_user() AS u")[0]["u"])

    def _columns(self, table: str) -> dict[str, str]:
        return {f.name: f.dataType.simpleString() for f in self.spark.table(table).schema.fields}

    def merge(self, binds: list[dict[str, Any]], *, llm_route: str) -> int:
        target = "recommendations_eval" if self.eval_run else "recommendations"
        recs = f"{self.signals}.{target}"
        has_route = "llm_route" in self._columns(recs)
        q = merge_sql(
            recs, f"{self.signals}.signal", llm_route=has_route, eval_run=bool(self.eval_run)
        )
        done = 0
        for b in binds:
            args = {**b, "llm_route": llm_route} if has_route else b
            if self.eval_run:
                args = {**args, "eval_run_id": self.eval_run}
            done += self._merge_one(q, args)
        return done

    def _merge_one(self, q: str, args: dict[str, Any]) -> int:
        try:
            return int(self._rows(q, args)[0].get("num_inserted_rows") or 0)
        except Exception as err:
            if "CONSTRAINT" not in str(err).upper():
                raise
        log.warning("CHECK rejected a row; retrying as constraint_rejected")
        retry = {**args, "downgrade": True, "downgrade_reason": "constraint_rejected"}
        return int(self._rows(q, retry)[0].get("num_inserted_rows") or 0)

    def run_ids_present(self, ids: Iterable[str]) -> set[str]:
        ids = list(ids)
        if not ids:
            return set()
        q = f"SELECT run_id FROM {self.signals}.agent_runs WHERE array_contains(:ids, run_id)"
        return {r["run_id"] for r in self._rows(q, {"ids": ids})}

    def insert_run(self, row: dict[str, Any]) -> None:
        table = f"{self.signals}.agent_runs"
        cols = self._columns(table)
        r = project(row, cols)
        sel = ", ".join(f"CAST(:{c} AS {cols[c]}) AS {c}" for c in r)
        names = ", ".join(r)
        vals = ", ".join(f"s.{c}" for c in r)
        q = (
            f"MERGE INTO {table} t USING (SELECT {sel}) s ON t.run_id = s.run_id "
            f"WHEN NOT MATCHED THEN INSERT ({names}) VALUES ({vals})"
        )
        self._rows(q, {c: (None if v is None else str(v)) for c, v in r.items()})


class FileStore:
    """A fixture hour on disk: inputs from JSON, writes appended to JSON lines beside it."""

    def __init__(self, fixture: Path):
        self.data = json.loads(fixture.read_text(encoding="utf-8"))
        self.out = Path(self.data.get("out") or fixture.parent / "out")
        self.out.mkdir(parents=True, exist_ok=True)

    def inputs(self, as_of: datetime) -> dict[str, dict[str, Any]]:
        return dict(self.data.get("inputs", {}))

    def prices(self) -> list[dict[str, Any]]:
        return list(self.data.get("prices", []))

    def guard(self, as_of: datetime) -> str:
        return str(self.data.get("guard", GUARD_OK))

    def writer(self) -> str:
        return str(self.data.get("writer", "fixture"))

    def merge(self, binds: list[dict[str, Any]], *, llm_route: str) -> int:
        """The bound values only; no action is computed here, as none is in the real MERGE's Python."""
        n = 0
        for b in binds:
            if str(b["target_date"]) not in self.data.get("inputs", {}):
                continue
            self._append("recommendations.jsonl", {**b, "llm_route": llm_route})
            n += 1
        return n

    def run_ids_present(self, ids: Iterable[str]) -> set[str]:
        path = self.out / "agent_runs.jsonl"
        have = (
            {json.loads(x)["run_id"] for x in path.read_text(encoding="utf-8").splitlines()}
            if path.exists()
            else set()
        )
        return have & set(ids)

    def insert_run(self, row: dict[str, Any]) -> None:
        if row["run_id"] not in self.run_ids_present([row["run_id"]]):
            self._append("agent_runs.jsonl", row)

    def _append(self, name: str, row: dict[str, Any]) -> None:
        runlog.append(self.out / name, row)


class EvalStore:
    """An evaluation hour: the fixture's inputs, written through the real `MERGE` into the eval table."""

    def __init__(self, spark: SparkStore, fixture: Path):
        self.spark, self.file = spark, FileStore(fixture)

    def inputs(self, as_of: datetime) -> dict[str, dict[str, Any]]:
        return self.file.inputs(as_of)

    def prices(self) -> list[dict[str, Any]]:
        return self.file.prices()

    def guard(self, as_of: datetime) -> str:
        return GUARD_OK

    def writer(self) -> str:
        return self.spark.writer()

    def merge(self, binds: list[dict[str, Any]], *, llm_route: str) -> int:
        return self.spark.merge(binds, llm_route=llm_route)

    def run_ids_present(self, ids: Iterable[str]) -> set[str]:
        return self.file.run_ids_present(ids)

    def insert_run(self, row: dict[str, Any]) -> None:
        self.file.insert_run(row)
