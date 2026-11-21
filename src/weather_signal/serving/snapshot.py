"""The hourly snapshot: the App's and the export's payloads, built from view rows.

`build` is pure: rows in, one dict out. `jobs.snapshot` reads the views and writes the file.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from weather_signal.config import PRIMARY
from weather_signal.serving import sources

log = logging.getLogger(__name__)
SCHEMA = 3
MAX_BYTES = 4 * 1024 * 1024
EVIDENCE_DAYS = 30
HISTORY_DAYS = 400
RUNS = 48
DEGRADE_DAYS = 7
UNAVAILABLE_RETRY = timedelta(hours=24)
HISTORY_COLUMNS = (
    "as_of",
    "target_date",
    "horizon_days",
    "signal_action",
    "signal_confidence",
    "forecast_anomaly_c",
    "shown_action",
    "source",
    "audit_problem",
    "last_close",
    "written_at",
)
INTRADAY_CALL_COLUMNS = (
    "as_of",
    "target_date",
    "horizon_days",
    "signal_action",
    "shown_action",
    "last_close",
    "est_price",
    "est_reason",
)
# A horizon-0 call belongs to a session's chart when made within this span of its first bar.
SESSION_SPAN = timedelta(hours=7)  # used only when a bar row lacks its session's close
AFTER_CLOSE = timedelta(hours=1)

Row = dict[str, Any]


def iso(v: Any) -> Any:
    """Timestamps as ISO 8601 with `Z` (naive values are UTC, as the session zone is)."""
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(UTC).replace(tzinfo=None)
        return v.isoformat(timespec="seconds") + "Z"
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, list | tuple):
        return [iso(x) for x in v]
    return v


def clean(rows: Iterable[Mapping[str, Any]]) -> list[Row]:
    return [{k: iso(v) for k, v in r.items()} for r in rows]


def evidence_cut(calls: list[Row], today: date) -> list[Row]:
    """Every row of the latest two `as_of`, plus the last `as_of` per (date, horizon) in 30 days."""
    as_ofs = sorted({c["as_of"] for c in calls if c.get("as_of")}, reverse=True)[:2]
    keep = [c for c in calls if c.get("as_of") in as_ofs]
    since = (today - timedelta(days=EVIDENCE_DAYS)).isoformat()
    last: dict[tuple[Any, Any], str] = {}
    for c in calls:
        if c.get("target_date") and c["target_date"] >= since:
            key = (c["target_date"], c.get("horizon_days"))
            last[key] = max(last.get(key, ""), c["as_of"] or "")
    keep += [
        c
        for c in calls
        if c.get("as_of") not in as_ofs
        and c.get("target_date")
        and last.get((c["target_date"], c.get("horizon_days"))) == c.get("as_of")
    ]
    return keep


def _summaries(
    evidence: list[Row], previous: Mapping[str, Any] | None, traces: Mapping[str, Row]
) -> dict[str, Row]:
    """Each summary still in the cut: newly searched first, else carried forward."""
    old = ((previous or {}).get("payload") or {}).get("traces") or {}
    out: dict[str, Row] = {}
    for c in evidence:
        tid = c.get("trace_id")
        if not tid:
            continue
        if tid in traces:
            out[tid] = traces[tid]
        elif tid in old:
            out[tid] = old[tid]
    return out


def wanted_traces(
    evidence: list[Row], previous: Mapping[str, Any] | None, now: datetime
) -> list[str]:
    """Trace ids to search: `tr-` ids not yet summarised, or `unavailable` and under 24 hours old."""
    old = ((previous or {}).get("payload") or {}).get("traces") or {}
    want = []
    for c in evidence:
        tid = c.get("trace_id")
        if not tid or not str(tid).startswith("tr-") or tid in want:
            continue
        have = old.get(tid)
        if have is None:
            want.append(tid)
        elif "unavailable" in have:
            written = _parse(c.get("written_at"))
            if written is None or now - written < UNAVAILABLE_RETRY:
                want.append(tid)
    return want


def _parse(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "")).replace(tzinfo=None)
    except ValueError:
        return None


def intraday(bars: Sequence[Mapping[str, Any]], calls: list[Row]) -> dict[str, list[Row]]:
    """Hourly bars (display only) and the horizon-0 calls made in their sessions."""
    rows = sorted(clean(bars), key=lambda b: b["bar_start"])
    span: dict[str, tuple[str, str]] = {}
    for b in rows:
        start = datetime.fromisoformat(b["bar_start"][:19])
        close = b.get("session_close")
        end = datetime.fromisoformat(close[:19]) + AFTER_CLOSE if close else start + SESSION_SPAN
        span.setdefault(b["bar_start"][:10], (iso(start), iso(end)))
    out = []
    for c in calls:
        bounds = span.get((c.get("as_of") or "")[:10])
        if c.get("horizon_days") != 0 or bounds is None:
            continue
        if bounds[0] <= c["as_of"] <= bounds[1]:
            out.append({k: c[k] for k in INTRADAY_CALL_COLUMNS})
    return {"bars": rows, "calls": sorted(out, key=lambda c: c["as_of"])}


def build(
    rows: Mapping[str, Sequence[Mapping[str, Any]]],
    traces: Mapping[str, Row],
    previous: Mapping[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    """The snapshot document; `now` is naive UTC, the job's read time."""
    calls = clean(rows["v_ui_calls"])
    status = clean(rows["v_ui_status"])
    evidence = evidence_cut(calls, now.date())
    since = (now.date() - timedelta(days=HISTORY_DAYS)).isoformat()
    # Only the newest call per date and horizon: all /api/calls and the history chart read.
    latest: dict[tuple[Any, Any], Row] = {}
    for c in calls:
        key = (c.get("target_date"), c.get("horizon_days"))
        if key[1] in (0, 6) and (key[0] or "") >= since:
            if key not in latest or c["as_of"] > latest[key]["as_of"]:
                latest[key] = c
    history = [{k: c.get(k) for k in HISTORY_COLUMNS} for _, c in sorted(latest.items())]
    index_rows = clean(rows["v_ui_index"])
    newest = max((r["date"] for r in index_rows if r.get("close") is not None), default=None)
    first = (
        (date.fromisoformat(newest) - timedelta(days=HISTORY_DAYS)).isoformat() if newest else ""
    )
    payload: dict[str, Any] = {
        "ticker": PRIMARY,
        "status": status[0] if status else None,
        "sources": clean(sources.strip(rows["v_ui_sources"], now)),
        "outlook": clean(rows["v_ui_outlook"]),
        "evidence": evidence,
        "history": history,
        "index": sorted((r for r in index_rows if r["date"] >= first), key=lambda r: r["date"]),
        "runs": sorted(clean(rows["v_ui_agent_runs"]), key=lambda r: r.get("started_at") or "")[
            -RUNS:
        ],
        "traces": _summaries(evidence, previous, traces),
        "intraday": intraday(rows.get("v_ui_intraday", []), calls),
    }
    read_at = status[0]["read_at"] if status else iso(now)
    return _fit(
        {"schema": SCHEMA, "built_at": iso(now), "read_at": read_at, "payload": payload}, now
    )


def _encode(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()


def _fit(doc: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Over 4 MB: drop evidence older than 7 days and log it; still over is an error."""
    if len(_encode(doc)) > MAX_BYTES:
        cutoff = (now.date() - timedelta(days=DEGRADE_DAYS)).isoformat()
        ev = doc["payload"]["evidence"]
        doc["payload"]["evidence"] = [c for c in ev if c.get("target_date", "") >= cutoff]
        log.warning("snapshot over %d bytes; dropped evidence older than 7 days", MAX_BYTES)
        doc["degraded"] = ["evidence older than 7 days"]
    body = _encode(doc)
    if len(body) > MAX_BYTES:
        raise ValueError(f"snapshot is {len(body)} bytes without old evidence; limit {MAX_BYTES}")
    doc["sha256"] = hashlib.sha256(_encode(doc)).hexdigest()
    return doc


def verify(doc: Mapping[str, Any]) -> bool:
    """True when `sha256` matches the document without it and the schema is this one."""
    body = {k: v for k, v in doc.items() if k != "sha256"}
    return doc.get("schema") == SCHEMA and hashlib.sha256(_encode(body)).hexdigest() == doc.get(
        "sha256"
    )
