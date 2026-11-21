"""Where the App's data comes from: the snapshot file, cached on its sha256, and a bounded live read."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any

from ws_ui.queries import SQL

log = logging.getLogger(__name__)
SCHEMA = 3
RECHECK_S = 60
LIVE_WAIT = "15s"


INTS = {"BYTE", "SHORT", "INT", "LONG"}
FLOATS = {"FLOAT", "DOUBLE", "DECIMAL"}
NESTED = {"ARRAY", "MAP", "STRUCT"}


def iso(v: str) -> str:
    """A warehouse timestamp (`2014-04-02T07:30:00.000Z`) in the snapshot's shape, `...:00Z`."""
    return v.replace(" ", "T")[:19] + "Z"


def typed(columns: list[tuple[str, str]], data: list[list[str | None]]) -> list[dict[str, Any]]:
    """Rows as the snapshot types them; the statement API returns every value as a string."""
    out = []
    for row in data:
        rec: dict[str, Any] = {}
        for (name, kind), v in zip(columns, row, strict=True):
            k = kind.upper()
            if v is None:
                rec[name] = None
            elif k in INTS:
                rec[name] = int(v)
            elif k in FLOATS:
                rec[name] = float(v)
            elif k == "BOOLEAN":
                rec[name] = v.lower() == "true"
            elif k.startswith("TIMESTAMP"):
                rec[name] = iso(v)
            elif k in NESTED:
                rec[name] = json.loads(v)
            else:
                rec[name] = v
        out.append(rec)
    return out


class Unavailable(RuntimeError):
    """No good snapshot and no live answer; the route says so rather than guessing."""


def _verify(doc: dict[str, Any]) -> bool:
    body = {k: v for k, v in doc.items() if k != "sha256"}
    enc = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return doc.get("schema") == SCHEMA and hashlib.sha256(enc).hexdigest() == doc.get("sha256")


class Source:
    def __init__(self) -> None:
        self.local = os.environ.get("WS_UI_LOCAL") == "1"
        self.catalog = os.environ.get("WS_UI_CATALOG", "workspace")
        self.schema = os.environ.get("WS_UI_SCHEMA", "app")
        self.warehouse = os.environ.get("WS_UI_WAREHOUSE_ID", "")
        self.path = os.environ.get(
            "WS_UI_SNAPSHOT", f"/Volumes/{self.catalog}/signals/ui/snapshot.json"
        )
        self._doc: dict[str, Any] | None = None
        self._checked = 0.0
        self._lock = threading.Lock()
        self._client: Any = None

    def client(self) -> Any:
        if self._client is None:
            from databricks.sdk import WorkspaceClient

            self._client = WorkspaceClient()
        return self._client

    def _download(self) -> dict[str, Any]:
        if self.local:
            with open(self.path, encoding="utf-8") as f:
                return dict(json.load(f))
        resp = self.client().files.download(self.path)
        return dict(json.loads(resp.contents.read()))

    def snapshot(self) -> dict[str, Any]:
        """The last good snapshot; re-read at most every 60 s, kept only when its sha256 checks."""
        with self._lock:
            if self._doc is None or time.monotonic() - self._checked > RECHECK_S:
                self._checked = time.monotonic()
                try:
                    doc = self._download()
                except Exception as err:  # noqa: BLE001 - keep the last good copy, log why
                    log.warning("snapshot read failed: %s", type(err).__name__)
                else:
                    if _verify(doc):
                        self._doc = doc
                    else:
                        log.warning(
                            "snapshot failed its sha256 or schema check; kept the last good"
                        )
            if self._doc is None:
                raise Unavailable("no snapshot has been read yet")
            return self._doc

    def live(self, key: str, params: dict[str, str]) -> list[dict[str, Any]]:
        """One fixed SELECT with bound parameters, 15 s, cancelled on timeout."""
        if self.local or not self.warehouse:
            raise Unavailable("live reads are off here")
        from databricks.sdk.service.sql import (
            ExecuteStatementRequestOnWaitTimeout,
            StatementParameterListItem,
            StatementState,
        )

        r = self.client().statement_execution.execute_statement(
            statement=SQL[key],
            warehouse_id=self.warehouse,
            catalog=self.catalog,
            schema=self.schema,
            parameters=[StatementParameterListItem(name=k, value=v) for k, v in params.items()],
            wait_timeout=LIVE_WAIT,
            on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CANCEL,
        )
        if r.status is None or r.status.state != StatementState.SUCCEEDED:
            raise Unavailable("warehouse_unavailable")
        cols = [
            (c.name, str(getattr(c.type_name, "value", c.type_name)))
            for c in r.manifest.schema.columns
        ]
        return typed(cols, self._all_chunks(r))

    def _all_chunks(self, r: Any) -> list[list[str | None]]:
        """A result past one inline chunk names the next; read each one in turn."""
        data = list((r.result.data_array if r.result else None) or [])
        nxt = r.result.next_chunk_index if r.result else None
        while nxt is not None:
            chunk = self.client().statement_execution.get_statement_result_chunk_n(
                r.statement_id, nxt
            )
            data += chunk.data_array or []
            nxt = chunk.next_chunk_index
        return data
