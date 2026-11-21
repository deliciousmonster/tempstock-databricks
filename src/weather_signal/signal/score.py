"""`score`: one `as_of` scored into the append-only `signals.signal` by an insert-only MERGE.

Each row carries the champion's estimated price for a buy or sell, or the reason it has none.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

from weather_signal import asof
from weather_signal.config import PRIMARY
from weather_signal.sessions import Calendar
from weather_signal.signal.champion import NO_CHAMPION, Deployed
from weather_signal.signal.estimate import Estimate, estimate
from weather_signal.signal.features import Features, Tables, build
from weather_signal.signal.model import HORIZON_CAP, BaseSignalModel, Signal, SignalInput


class DuplicateKey(AssertionError):
    """Two rows for one (as_of, target_date): the MERGE would insert both."""


def frame(
    as_of: datetime,
    p_hours: int,
    tables: Tables,
    cal: Calendar,
    model: BaseSignalModel,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    day = asof.decision_day(as_of, p_hours)
    rows = []
    for k in asof.horizons(as_of, p_hours):
        target = asof.target_date(day, k)
        if k >= HORIZON_CAP:
            f = None
            sig = Signal("hold", 0.0, 0.0, ("horizon_unavailable",))
        else:
            f = build(day, target, tables, as_of=as_of, cal=cal)
            sig = model.predict(SignalInput(as_of, target, k, f.values, f.inputs_known_at or as_of))
        est = _estimate(sig, model, f, target, cal)
        rows.append(row(as_of, target, k, sig, model, f, now) | est.columns())
    return rows


def _estimate(
    sig: Signal, model: BaseSignalModel, f: Features | None, target: date, cal: Calendar
) -> Estimate:
    champ = model if isinstance(model, Deployed) and NO_CHAMPION not in sig.reasons else None
    extra = f.extra if f else {}
    close = None
    if "last_close" in extra:
        close = (float(str(extra["last_close"])), date.fromisoformat(str(extra["last_close_date"])))
    return estimate(
        sig.action,
        fit_as_of=champ.fit_as_of if champ else None,
        basis=champ.basis if champ else None,
        close=close,
        target=target,
        cal=cal,
    )


def row(
    as_of: datetime,
    target: date,
    k: int,
    sig: Signal,
    model: BaseSignalModel,
    f: Features | None,
    now: datetime | None,
) -> dict[str, Any]:
    """A scored row's leading columns, in the table's order; each table appends its own."""
    features = {**f.values, **f.extra} if f else {}
    return {
        "as_of": as_of,
        "target_date": target,
        "horizon_days": k,
        "action": sig.action,
        "score": float(sig.score),
        "confidence": float(sig.confidence),
        "reasons": list(sig.reasons),
        "model_name": model.model_name,
        "model_version": model.model_version,
        "features": strict_json(features),
        "inputs_max_known_at": f.inputs_known_at if f else None,
        "scored_at": now or datetime.now(UTC),
        "ticker": PRIMARY,
    }


def strict_json(features: dict[str, Any]) -> str:
    """NaN and infinity as null: Spark's `get_json_object` reads a document with a bare NaN as null."""
    clean = {
        k: None if isinstance(v, float) and not math.isfinite(v) else v for k, v in features.items()
    }
    return json.dumps(clean, default=str, allow_nan=False)


def assert_unique(rows: Iterable[dict[str, Any]]) -> None:
    counts = Counter((r["as_of"], r["target_date"]) for r in rows)
    dupes = [k for k, n in counts.items() if n > 1]
    if dupes:
        raise DuplicateKey(f"{len(dupes)} duplicated keys, first {dupes[0]}")


def merge_sql(signal_table: str, source_view: str) -> str:
    return (
        f"MERGE INTO {signal_table} t USING {source_view} s "
        "ON t.as_of = s.as_of AND t.target_date = s.target_date "
        "WHEN NOT MATCHED THEN INSERT *"
    )
