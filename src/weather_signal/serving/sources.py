"""The Sources strip: each `v_ui_sources` row with a state judged at render against its cadence."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from weather_signal.signal import checks

# Keyed by the foundation's own `source` strings (ingest/hourly.py, backfill.py, yahoo_history.py,
# intraday.py).
CADENCE: dict[str, timedelta | None] = {
    "alphavantage": timedelta(hours=1),
    "open-meteo-previous-runs": timedelta(hours=1),
    "open-meteo-era5": timedelta(hours=24),
    "yahoo": None,
    "yahoo-intraday": None,
}
# Fetched only in session hours and never a model input, so its age says nothing about staleness.
DISPLAY_ONLY = frozenset({"yahoo-intraday"})
# States whose label carries the last landing's date.
DATED = frozenset({"backfill", "display only"})
# A signals freshness row carries the newest data day, not a landing time.
DAY_DATASETS = frozenset({"temp_index_daily"})
# Expectations that count by design, keyed to the check that calls a run a fill
# (FILL_ALERT_METROS metros in one file and lead); see `_counted_label`.
COUNTED = {"forecast_fill_check": "forecast_fill_summary"}
# views._sources: metro-lead cells (n) and metros (n_bad) in the file that quarantined the most.
SHAPE = "fill_shape"


def source_state(name: str, at: datetime | None, now: datetime) -> str:
    if name not in CADENCE:
        return "unlabelled source"
    if at is None:
        return "not landed"
    if name in DISPLAY_ONLY:
        return "display only"
    cadence = CADENCE[name]
    if cadence is None:
        return "backfill"
    return "stale" if now - at > 2 * cadence else "ok"


def _clean_checks(rows: list[Mapping[str, Any]]) -> set[Any]:
    return {
        r.get("name")
        for r in rows
        if r.get("kind") == "expectations" and r.get("state") == "COMPLETED" and r.get("n_bad") == 0
    }


def _counted_label(name: str, rows: list[Mapping[str, Any]]) -> tuple[bool, str] | None:
    """Weather only when the fill check ran clean and each recent file set aside one metro at one
    lead; files older than the alert window rest on the summary alone."""
    shape = next((r for r in rows if r.get("kind") == SHAPE), None)
    if COUNTED[name] not in _clean_checks(rows) or shape is None or shape.get("n") is None:
        return None
    cells, metros = int(shape["n"]), int(shape.get("n_bad") or 0)
    days, bar = checks.FILL_ALERT_WINDOW.days, checks.FILL_ALERT_METROS
    if cells > 1:
        noun = "metro" if metros == 1 else "metros"
        return (
            False,
            f"worst file in the last {days} days: {metros} {noun} over {cells} metro-lead cells",
        )
    recent = (
        f"no file quarantined in the last {days} days"
        if cells == 0
        else f"one metro at one lead in each file of the last {days} days"
    )
    return True, f"{recent}, older runs under the {bar}-metro fill bar; set aside as weather"


def strip(rows: Iterable[Mapping[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Rows of `v_ui_sources` with `state` and `label` set; a `CADENCE` key with no row is added."""
    rows = list(rows)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if row.get("kind") == SHAPE:
            continue
        r = dict(row, counted=False)
        kind, name = r.get("kind"), r.get("name")
        if kind == "source":
            seen.add(str(name))
            r["state"] = source_state(str(name), r.get("at"), now)
            r["label"] = f"{r['state']}, {r['at']:%Y-%m-%d}" if r["state"] in DATED else r["state"]
        elif kind == "fresh":
            r["label"] = "latest data day" if name in DAY_DATASETS else "latest known_at"
        elif kind in ("update", "expectations"):
            clean = r.get("state") == "COMPLETED"
            r["label"] = "checked" if clean else f"pipeline update {r.get('state') or 'unknown'}"
            said = _counted_label(str(name), rows) if clean and name in COUNTED else None
            if said:
                r["counted"], r["label"] = said
        out.append(r)
    for name in CADENCE:
        if name not in seen:
            out.append(
                {
                    "kind": "source",
                    "layer": "raw",
                    "name": name,
                    "at": None,
                    "state": "not landed",
                    "label": "not landed",
                    "n": None,
                    "n_bad": None,
                    "counted": False,
                }
            )
    return out
