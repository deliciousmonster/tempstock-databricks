"""Synthetic forecast and price tables with `known_at` from the plan's rules."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from tests.unit.av_sim import served, simulate
from weather_signal import asof
from weather_signal.config import TICKERS
from weather_signal.sessions import Calendar


def forecast(start: date, days: int, p_hours: int) -> pd.DataFrame:
    rows = []
    for i in range(days):
        t = start + timedelta(days=i)
        last = datetime(t.year, t.month, t.day, 23, tzinfo=asof.UTC)
        for lead in asof.LEADS:
            rows.append(
                {
                    "target_date": t,
                    "lead_days": lead,
                    "anomaly_c": i * 0.01 + lead,
                    "known_at": asof.known_at_forecast(last, lead, p_hours),
                }
            )
    return pd.DataFrame(rows)


def _close(ticker: str, d: date) -> float:
    return 100.0 + TICKERS.index(ticker) * 50 + d.toordinal() % 97


def prices_at_close(cal: Calendar, start: date, end: date, lag_h: float = 1.0) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ticker": t,
                "trade_date": d,
                "close": _close(t, d),
                "known_at": cal.close(d) + timedelta(hours=lag_h),
            }
            for d in cal.between(start, end)
            for t in TICKERS
        ]
    )


def prices_from_ledger(cal: Calendar, start: datetime, hours: int, lag_h: float) -> pd.DataFrame:
    """First-seen bars from the real slot ledger: each landed compact call refills 100 sessions."""
    reports, _ = simulate(cal, start, hours, lag_h)
    seen: dict[tuple[str, date], datetime] = {}
    for _, r in reports:
        if r.outcome != "landed" or r.sent_at is None:
            continue
        newest = served(cal, r.sent_at, lag_h)
        d = newest
        for _ in range(100):
            seen.setdefault((r.ticker, d), r.sent_at)
            d = cal.previous(d)
    return pd.DataFrame(
        [
            {"ticker": t, "trade_date": d, "close": _close(t, d), "known_at": k}
            for (t, d), k in seen.items()
        ]
    )


def observed_until(first: date, last: date, anomaly_c: float) -> pd.DataFrame:
    """ERA5 days `first`..`last`, each known 8 days after its day; nothing after `last` lands."""
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    rows = [(d, anomaly_c, asof.known_at_era5_backfill(d)) for d in days]
    return pd.DataFrame(rows, columns=["date", "anomaly_c", "known_at"])
