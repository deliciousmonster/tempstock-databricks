"""Requests for each source, built so the envelope and every log line carry no credential."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from importlib import resources
from pathlib import Path

from weather_signal.ingest.fetch import (
    ONE_SHOT,
    OPEN_METEO,
    RawResponse,
    RetryPolicy,
    Transport,
    fetch,
)

PREVIOUS_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
ALPHA_VANTAGE = "https://www.alphavantage.co/query"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HOURLY_VARS = ["temperature_2m"] + [f"temperature_2m_previous_day{n}" for n in range(1, 8)]
DAILY_VARS = ["temperature_2m_mean", "temperature_2m_max", "temperature_2m_min"]


@dataclass(frozen=True)
class Metro:
    rank: int
    cbsa_code: str
    weight: float
    latitude: float
    longitude: float


def metros(path: Path | None = None) -> list[Metro]:
    if path is None:
        ref = resources.files("weather_signal").joinpath("seed/metros.csv")
        text = ref.read_text(encoding="utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    rows = csv.DictReader(text.splitlines())
    out = [
        Metro(
            int(r["rank"]),
            r["cbsa_code"],
            float(r["weight"]),
            float(r["latitude"]),
            float(r["longitude"]),
        )
        for r in rows
    ]
    return sorted(out, key=lambda m: m.rank)


def _coords(ms: list[Metro]) -> dict[str, str]:
    return {
        "latitude": ",".join(f"{m.latitude:.4f}" for m in ms),
        "longitude": ",".join(f"{m.longitude:.4f}" for m in ms),
    }


def previous_runs_params(
    ms: list[Metro], start: date | None = None, end: date | None = None
) -> dict[str, str]:
    p = {
        **_coords(ms),
        "hourly": ",".join(HOURLY_VARS),
        "models": "gfs_seamless",
        "timezone": "GMT",
    }
    if start and end:
        return p | {"start_date": start.isoformat(), "end_date": end.isoformat()}
    return p | {"forecast_days": "8", "past_days": "1"}


def era5_params(ms: list[Metro], start: date, end: date) -> dict[str, str]:
    return {
        **_coords(ms),
        "daily": ",".join(DAILY_VARS),
        "models": "era5",
        "timezone": "GMT",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }


def open_meteo(
    url: str, params: dict[str, str], transport: Transport, policy: RetryPolicy = OPEN_METEO
) -> RawResponse:
    return fetch("open-meteo", url, params, policy=policy, transport=transport)


@dataclass
class AlphaVantageDaily:
    """One attempt per slot; the key goes only into the request it signs."""

    key: str
    transport: Transport
    source = "alphavantage"
    endpoint = ALPHA_VANTAGE

    def params(self, ticker: str) -> dict[str, str]:
        return {
            "function": "TIME_SERIES_DAILY",
            "symbol": ticker,
            "outputsize": "compact",
            "apikey": self.key,
        }

    def fetch(self, ticker: str) -> RawResponse:
        return fetch(
            self.source,
            self.endpoint,
            self.params(ticker),
            policy=ONE_SHOT,
            transport=self.transport,
        )


@dataclass
class YahooChartDaily:
    transport: Transport
    source = "yahoo"

    def params(self, start: date, end: date) -> dict[str, str]:
        p1 = int(datetime.combine(start, time(0), tzinfo=UTC).timestamp())
        p2 = int(datetime.combine(end, time(23, 59), tzinfo=UTC).timestamp())
        return {"period1": str(p1), "period2": str(p2), "interval": "1d"}

    def fetch(self, ticker: str, start: date, end: date) -> RawResponse:
        return fetch(
            self.source,
            YAHOO.format(ticker=ticker),
            self.params(start, end),
            policy=ONE_SHOT,
            transport=self.transport,
        )


def hourly_columns(body: object) -> dict[int, list[float | None]]:
    """Lead columns per location body; a column with no value at all is absent, not data."""
    if not isinstance(body, dict):
        return {}
    hourly = body.get("hourly") or {}
    out: dict[int, list[float | None]] = {}
    for name, values in hourly.items():
        if not name.startswith("temperature_2m_previous_day"):
            continue
        if any(v is not None for v in values):
            out[int(name.removeprefix("temperature_2m_previous_day"))] = values
    return out


def daily_columns(body: object) -> dict[str, list[float | None]]:
    if not isinstance(body, dict):
        return {}
    daily = body.get("daily") or {}
    return {k: v for k, v in daily.items() if k != "time" and any(x is not None for x in v)}
