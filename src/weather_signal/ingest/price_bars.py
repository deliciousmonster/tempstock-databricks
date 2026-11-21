"""Parse Alpha Vantage and Yahoo daily bodies into one bar shape; unadjusted closes from both."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from weather_signal.sessions import COMPLETE_AFTER, Calendar


@dataclass(frozen=True)
class Bar:
    ticker: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float


def alpha_vantage(ticker: str, doc: dict[str, Any]) -> list[Bar]:
    series = doc["Time Series (Daily)"]
    return sorted(
        (
            Bar(
                ticker,
                date.fromisoformat(d),
                float(v["1. open"]),
                float(v["2. high"]),
                float(v["3. low"]),
                float(v["4. close"]),
                float(v["5. volume"]),
            )
            for d, v in series.items()
        ),
        key=lambda b: b.trade_date,
    )


def yahoo(ticker: str, doc: dict[str, Any]) -> list[Bar]:
    res = doc["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res["timestamp"]):
        if q["close"][i] is None:
            continue
        out.append(
            Bar(
                ticker,
                datetime.fromtimestamp(ts, UTC).date(),
                float(q["open"][i]),
                float(q["high"][i]),
                float(q["low"][i]),
                float(q["close"][i]),
                float(q["volume"][i] or 0),
            )
        )
    return out


def complete(trade_date: date, fetched_at: datetime, cal: Calendar) -> bool:
    """A bar is complete once its session closed at least 15 minutes before the fetch."""
    return fetched_at >= cal.close(trade_date) + COMPLETE_AFTER


def from_envelope(source: str, endpoint: str, params: dict[str, str], body_text: str) -> list[Bar]:
    """Every bar one landed envelope holds, unfiltered, so the pipeline's bounds see a forgery."""
    import json

    doc = json.loads(body_text)
    if source == "alphavantage":
        bars = alpha_vantage(params["symbol"], doc)
    elif source == "yahoo":
        bars = yahoo(endpoint.rstrip("/").rsplit("/", 1)[1], doc)
    else:
        return []
    return bars
