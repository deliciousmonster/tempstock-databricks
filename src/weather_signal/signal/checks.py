"""The `check` task's metrics as findings: each warns or fails on its own threshold."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

Level = Literal["warn", "fail"]
DISAGREE_FAIL = 5
CALENDAR_WARN = timedelta(days=180)
FILL_ALERT_METROS = 5
FILL_ALERT_WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class Finding:
    level: Level
    name: str
    detail: str


def disagreements(count_30_sessions: int) -> list[Finding]:
    if count_30_sessions > DISAGREE_FAIL:
        return [Finding("fail", "price_disagreements", f"{count_30_sessions} over 30 sessions")]
    return []


def calendar_end(today: date, end: date) -> list[Finding]:
    if end - today <= CALENDAR_WARN:
        return [Finding("warn", "calendar_end", f"session calendar ends {end}")]
    return []


def as_utc(t: datetime) -> datetime:
    """Spark's `collect()` gives naive timestamps; the tables hold UTC."""
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t


def forecast_filled(
    summary: Iterable[Mapping[str, object]], now: datetime, alerted: Collection[str] = ()
) -> list[Finding]:
    """A lead quarantined at FILL_ALERT_METROS or more metros in one recent envelope is a fill,
    reported once: an envelope in `alerted` was reported by an earlier run."""
    out = []
    for r in (r for r in summary if r["path"] not in alerted):
        recent = as_utc(r["fetched_at"]) >= now - FILL_ALERT_WINDOW  # type: ignore[arg-type]
        if recent and int(r["n_metros"]) >= FILL_ALERT_METROS:  # type: ignore[call-overload]
            detail = (
                f"lead {r['lead_days']} at {r['n_metros']} metros, {r['hours']} h in {r['path']}"
            )
            out.append(Finding("fail", "forecast_filled", detail))
    return out


def verdict(findings: Iterable[Finding]) -> str:
    levels = {f.level for f in findings}
    return "fail" if "fail" in levels else ("warn" if levels else "ok")
