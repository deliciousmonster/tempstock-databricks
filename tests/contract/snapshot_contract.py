"""What the App (main.py, integrity) and the page (app.js) read from snapshot.json, schema 3."""

from __future__ import annotations

import re
from typing import Any

CALL = {
    "as_of",
    "target_date",
    "horizon_days",
    "signal_action",
    "reasons",
    "shown_action",
    "forecast_anomaly_c",
    "downgrade_reason",
    "run_outcome",
    "signal_confidence",
    "forecast_known_at",
    "last_close",
    "ret_5d",
    "model_name",
    "model_version",
    "audit_problem",
    "written_at",
    "rationale",
    "trace_id",
}
SECTIONS = {
    "outlook": CALL,
    "evidence": CALL,
    "history": {
        "as_of",
        "target_date",
        "horizon_days",
        "signal_action",
        "signal_confidence",
        "forecast_anomaly_c",
        "shown_action",
        "audit_problem",
        "written_at",
    },
    "index": {"date", "close", "anomaly_c"},
    "runs": {"as_of", "started_at", "route", "outcome", "duration_s"},
    # v_ui_sources gives every row all eight and strip adds `counted`; app.js reads each by kind.
    "sources": {"kind", "name", "state", "label", "at", "layer", "n", "n_bad", "counted"},
}
# The intraday page (app.js `intraday`): display-only bars, and the session calls with est_price,
# which is null with est_reason naming why when there is none.
INTRADAY = {
    "bars": {"ticker", "bar_start", "close"},
    "calls": {
        "as_of",
        "target_date",
        "shown_action",
        "signal_action",
        "last_close",
        "est_price",
        "est_reason",
    },
}
STATUS = {"open_incident", "post_verified_as_of", "guarded_tables", "read_at"}
AS_OF = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00Z")
TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def problems(doc: dict[str, Any]) -> list[str]:
    out = []
    if doc.get("schema") != 3 or not TS.fullmatch(doc.get("built_at") or ""):
        out.append("schema or built_at")
    p = doc["payload"]
    if not STATUS <= set(p["status"] or {}):
        out.append(f"status lacks {sorted(STATUS - set(p['status'] or {}))}")
    for name, need in SECTIONS.items():
        for i, row in enumerate(p[name]):
            if not need <= set(row):
                out.append(f"{name}[{i}] lacks {sorted(need - set(row))}")
    for name in ("outlook", "evidence", "history"):
        for row in p[name]:
            if not AS_OF.fullmatch(row["as_of"]) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", row["target_date"]
            ):
                out.append(f"{name} as_of/target_date shape: {row['as_of']} {row['target_date']}")
            if row.get("written_at") is not None and not TS.fullmatch(row["written_at"]):
                out.append(f"{name} written_at shape: {row['written_at']}")
    intraday = p.get("intraday")
    if not isinstance(intraday, dict):
        out.append("intraday is not a mapping")
    else:
        for name, need in INTRADAY.items():
            for i, row in enumerate(intraday.get(name, [])):
                if not need <= set(row):
                    out.append(f"intraday.{name}[{i}] lacks {sorted(need - set(row))}")
                at = row.get("bar_start" if name == "bars" else "as_of")
                if not TS.fullmatch(at or ""):
                    out.append(f"intraday.{name}[{i}] time shape: {at}")
    if not isinstance(p["traces"], dict):
        out.append("traces is not a mapping")
    return out
