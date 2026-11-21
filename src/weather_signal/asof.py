"""When a value became knowable, and what a decision at `as_of` may see."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pandas as pd

ERA5_BACKFILL_DELAY = timedelta(days=8)
# An observed day this much older than the session is a frozen input, not persistence (2008-2013 gap).
MAX_OBSERVED_AGE = timedelta(days=14)
BOUNDARY_MARGIN_H = 2.0
LEADS = range(1, 8)


class LookaheadError(AssertionError):
    """A row known after the decision instant reached a decision."""


def utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


# Grid -----------------------------------------------------------------------------------------


def live_as_of(now: datetime) -> datetime:
    """The `:30` grid point of the hour `now` falls in."""
    now = utc(now)
    return now.replace(minute=30, second=0, microsecond=0)


def backtest_as_of(session: date) -> datetime:
    """Backtest decisions sit at 21:30 UTC on each session, after every close."""
    return datetime.combine(session, time(21, 30), tzinfo=UTC)


# known_at per source --------------------------------------------------------------------------


def known_at_forecast(valid_time: datetime, lead_days: int, p_hours: int) -> datetime:
    """A lead-N value for hour v came from a run started no later than v - 24N h, served P later."""
    return utc(valid_time) - timedelta(hours=24 * lead_days) + timedelta(hours=p_hours)


def known_at_era5_backfill(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=UTC) + ERA5_BACKFILL_DELAY


# Visibility -----------------------------------------------------------------------------------


def visible(
    df: pd.DataFrame, as_of: datetime, keys: Sequence[str], order: str = "known_at"
) -> pd.DataFrame:
    """Keep rows known at `as_of`, and the latest of them per key."""
    if df.empty:
        return df
    kept = df[pd.to_datetime(df["known_at"], utc=True) <= pd.Timestamp(utc(as_of))]
    if kept.empty:
        return kept
    return kept.sort_values(order).groupby(list(keys), as_index=False).tail(1)


def observed_stale(target: date, observed: date) -> bool:
    """An observed day older than `MAX_OBSERVED_AGE` at the target must not stand in for a forecast."""
    return target - observed > MAX_OBSERVED_AGE


def assert_no_lookahead(rows: Iterable[Mapping[str, Any]], as_of: datetime) -> None:
    limit = utc(as_of)
    for row in rows:
        if utc(pd.Timestamp(row["known_at"]).to_pydatetime()) > limit:
            raise LookaheadError(f"row known at {row['known_at']} used at {limit.isoformat()}")


# The forecast publication delay P ------------------------------------------------------------


def lead_boundary(body: Mapping[str, Any], fetched_at: datetime) -> dict[int, float]:
    """Hours from the last real lead-N value's run bound (v - 24N h) to the fetch, per lead."""
    hourly = body["hourly"]
    times = [datetime.fromisoformat(t).replace(tzinfo=UTC) for t in hourly["time"]]
    lead0 = hourly["temperature_2m"]
    out: dict[int, float] = {}
    for n in LEADS:
        values = hourly.get(f"temperature_2m_previous_day{n}")
        if values is None:
            continue
        k = len(times)
        while k > 0 and (values[k - 1] is None or values[k - 1] == lead0[k - 1]):
            k -= 1
        if k == 0:
            continue
        bound = times[k - 1] - timedelta(hours=24 * n)
        out[n] = (utc(fetched_at) - bound).total_seconds() / 3600
    return out


def p_from_samples(samples_h: Iterable[float]) -> int:
    """P is the largest boundary seen plus the margin, rounded up to a whole hour, with no floor."""
    return math.ceil(max(samples_h) + BOUNDARY_MARGIN_H)


# Decision day ---------------------------------------------------------------------------------


def _shift(p_hours: int) -> timedelta:
    return timedelta(hours=max(0, p_hours - 25))


def decision_day(as_of: datetime, p_hours: int) -> date:
    return (utc(as_of) - _shift(p_hours)).date()


def horizons(as_of: datetime, p_hours: int) -> list[int]:
    """Horizons whose target date is not before the UTC date of `as_of`."""
    d = decision_day(as_of, p_hours)
    today = utc(as_of).date()
    return [k for k in range(7) if d + timedelta(days=k) >= today]


def target_date(day: date, horizon: int) -> date:
    return day + timedelta(days=horizon)


def fitted_lead(horizon: int) -> int:
    return horizon + 2


def landed_values(
    body: Mapping[str, Any], fetched_at: datetime, p_hours: int
) -> list[tuple[datetime, int, float]]:
    """The (valid hour, lead, value) a fetch keeps: non-null and known at or before the fetch."""
    hourly = body["hourly"]
    out = []
    for n in LEADS:
        for t, v in zip(
            hourly["time"], hourly.get(f"temperature_2m_previous_day{n}") or [], strict=False
        ):
            vt = datetime.fromisoformat(t).replace(tzinfo=UTC)
            if v is not None and known_at_forecast(vt, n, p_hours) <= utc(fetched_at):
                out.append((vt, n, float(v)))
    return out
