"""`weather-signal-ui-snapshot`: read the six views and the intraday one, summarise new traces, write `snapshot.json`."""

from __future__ import annotations

import argparse
import io
import json
import logging
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from weather_signal.config import Names
from weather_signal.serving import snapshot, views

log = logging.getLogger(__name__)
TRACE_BOUND_S = 60


def snapshot_path(n: Names) -> str:
    return f"{n.volume_path('signals', 'ui')}/snapshot.json"


def read_views(spark: Any, n: Names) -> dict[str, list[dict[str, Any]]]:
    app = n.schema("app")
    out = {
        v: [r.asDict(recursive=True) for r in spark.sql(f"SELECT * FROM {app}.{v}").collect()]
        for v in views.VIEWS
    }
    out[views.INTRADAY_VIEW] = read_intraday(spark, n)
    return out


def read_intraday(spark: Any, n: Names) -> list[dict[str, Any]]:
    """Display-only bars; a missing or unreadable view is an empty chart, never a failed snapshot."""
    try:
        rows = spark.sql(f"SELECT * FROM {n.table('app', views.INTRADAY_VIEW)}").collect()
    except Exception as err:  # noqa: BLE001 - the chart is optional; the rest of the snapshot is not
        log.warning("intraday not read: %s", type(err).__name__)
        return []
    return [r.asDict(recursive=True) for r in rows]


def read_previous(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as err:
        log.warning("no previous snapshot at %s: %s", path, err)
        return None
    return doc if snapshot.verify(doc) else None


def summarise(trace: Any) -> dict[str, Any]:
    """Tool span names in order, input keys (not values) and durations; nothing of the content."""
    spans = sorted(trace.data.spans, key=lambda s: s.start_time_ns)
    return {
        "spans": [
            {
                "name": s.name,
                "type": str(getattr(s, "span_type", "") or ""),
                "input_keys": sorted((s.inputs or {}).keys()) if isinstance(s.inputs, dict) else [],
                "ms": round(((s.end_time_ns or s.start_time_ns) - s.start_time_ns) / 1e6),
            }
            for s in spans
        ]
    }


def search_traces(ids: list[str], bound_s: float = TRACE_BOUND_S) -> dict[str, dict[str, Any]]:
    """One read per new trace id, inside one bound; what is not read is `unavailable` with why."""
    out: dict[str, dict[str, Any]] = {}
    try:
        import mlflow
    except ImportError as err:
        return {t: {"unavailable": f"mlflow not installed: {err}"} for t in ids}
    deadline = time.monotonic() + bound_s
    for tid in ids:
        if time.monotonic() > deadline:
            out[tid] = {"unavailable": f"search past {bound_s:.0f} s; retried next build"}
            continue
        try:
            trace = mlflow.get_trace(tid)
            out[tid] = summarise(trace) if trace else {"unavailable": "trace not found"}
        except Exception as err:  # noqa: BLE001 - any failure is recorded per trace, not raised
            out[tid] = {"unavailable": f"{type(err).__name__}: {str(err)[:200]}"}
    return out


def write(path: str, doc: dict[str, Any]) -> None:
    from databricks.sdk import WorkspaceClient

    body = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    WorkspaceClient().files.upload(path, io.BytesIO(body), overwrite=True)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="weather-signal-ui-snapshot")
    ap.add_argument("--catalog", default="workspace")
    ap.add_argument("--schema-prefix", default="")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    from pyspark.sql import SparkSession

    n = Names(a.catalog, a.schema_prefix)
    spark = SparkSession.builder.getOrCreate()
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = read_views(spark, n)
    path = snapshot_path(n)
    previous = read_previous(path)
    calls = snapshot.clean(rows["v_ui_calls"])
    ids = snapshot.wanted_traces(snapshot.evidence_cut(calls, now.date()), previous, now)
    doc = snapshot.build(rows, search_traces(ids), previous, now)
    write(path, doc)
    log.info(
        "wrote %s: %d evidence rows, %d traces searched",
        path,
        len(doc["payload"]["evidence"]),
        len(ids),
    )
    return 0
