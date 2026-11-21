"""The population-weighted temperature index and its normals over 1961 to 1990, every year present.

The window ends before the primary's first session (1991-01-02), so no normal draws on a scored year.
An archive missing any of those years takes its normals from the training half instead: the index
days before the middle session with both a close and an index, the split `train` uses.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

import pandas as pd

N_METROS = 50
NORMALS_YEARS = (1961, 1990)
MIN_FORECAST_WEIGHT = 0.97


def expected_years(day_of_year: int) -> int:
    """Window years that have this day of year: all of them, or the leap years for day 366."""
    lo, hi = NORMALS_YEARS
    if day_of_year < 366:
        return hi - lo + 1
    return sum(calendar.isleap(y) for y in range(lo, hi + 1))


def training_half(index_daily: pd.DataFrame, closes: pd.Series) -> tuple[date, date] | None:
    """The first session with both a close and an index, and the middle one, which the half excludes."""
    both = sorted(
        set(pd.to_datetime(index_daily["date"]).dt.date) & set(pd.to_datetime(closes).dt.date)
    )
    return (both[0], both[len(both) // 2]) if len(both) >= 2 else None


def period_label(first: date, mid: date) -> str:
    return f"{first.year}-{(mid - timedelta(days=1)).year} (training half)"


def normals(index_daily: pd.DataFrame, closes: pd.Series | None = None) -> pd.DataFrame:
    """Mean index per day of year over 1961-1990, null unless every expected year is present; with
    `closes` and a year of that window missing, the training half's mean, any year counting."""
    d = pd.to_datetime(index_daily["date"])
    lo, hi = NORMALS_YEARS
    half = None if closes is None else training_half(index_daily, closes)
    if half and d[(d.dt.year >= lo) & (d.dt.year <= hi)].dt.year.nunique() < hi - lo + 1:
        first, mid = half
        g = index_daily[(d.dt.date >= first) & (d.dt.date < mid)]
        g = g.assign(day_of_year=pd.to_datetime(g["date"]).dt.day_of_year)
        out = g.groupby("day_of_year", as_index=False).agg(normal_c=("index_c", "mean"))
        return out.assign(period=period_label(first, mid))
    window = index_daily[(d.dt.year >= lo) & (d.dt.year <= hi)]
    when = pd.to_datetime(window["date"])
    g = window.assign(day_of_year=when.dt.day_of_year, year=when.dt.year)
    out = g.groupby("day_of_year", as_index=False).agg(
        normal_c=("index_c", "mean"), n_years=("year", "nunique")
    )
    full = out["n_years"] >= out["day_of_year"].map(expected_years)
    return out.assign(normal_c=out["normal_c"].where(full), period=f"{lo}-{hi}")
