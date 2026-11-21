"""Synthetic Yahoo chart bodies in the 60m shape measured once on 2026-11-20 (shape only, no values).

`timestamp` holds each bar's start (14:30Z, 15:30Z, ...) and, last, a quote stamped at the fetch;
`indicators.quote[0]` holds open, high, low, close and volume arrays of the same length.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from weather_signal.sessions import Calendar


def body(points: list[tuple[datetime, float | None]]) -> bytes:
    q: dict[str, list[float | int | None]] = {
        k: [] for k in ("open", "high", "low", "close", "volume")
    }
    for _, c in points:
        q["open"].append(None if c is None else c - 0.1)
        q["high"].append(None if c is None else c + 0.5)
        q["low"].append(None if c is None else c - 0.5)
        q["close"].append(c)
        q["volume"].append(None if c is None else 1000)
    doc = {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": "WMT", "dataGranularity": "60m", "range": "5d"},
                    "timestamp": [int(t.timestamp()) for t, _ in points],
                    "indicators": {"quote": [q]},
                }
            ],
            "error": None,
        }
    }
    return json.dumps(doc).encode()


def session_points(cal: Calendar, days: list, until: datetime) -> list[tuple[datetime, float]]:
    """Every hour-grid bar start of `days` before `until`, closes 100.0, 100.1, ..."""
    out = []
    for d in days:
        s = cal.by_day[d]
        t = s.open
        while t < s.close and t < until:
            out.append((t, round(100 + len(out) / 10, 4)))
            t += timedelta(hours=1)
    return out
