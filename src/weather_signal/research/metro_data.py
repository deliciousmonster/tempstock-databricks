"""Per-metro anomalies against day-of-year normals built before a cutoff, and the stock's targets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from weather_signal.config import BENCHMARKS, PRIMARY, key

HS = (1, 5)
TARGETS = tuple(
    f"{name}_{h}d" for name in (key(PRIMARY), *(f"xs_{key(b)}" for b in BENCHMARKS)) for h in HS
)
LAGS = tuple(range(6))
KINDS = ("level", "change")


def day_of_year(dates: pd.Series | pd.Index) -> np.ndarray:
    """1..365 with Feb 29 folded onto Feb 28, so leap years line up with the rest."""
    d = pd.DatetimeIndex(dates)
    n = d.day_of_year.to_numpy()
    return np.asarray(n - (d.is_leap_year & (n >= 60)), dtype=int)


@dataclass(frozen=True)
class Normals:
    table: pd.DataFrame  # index day of year 1..365, one column per metro
    n_years: pd.Series  # distinct years behind each metro's normal
    cutoff: date


def normals_before(obs: pd.DataFrame, cutoff: date, half_window: int = 7) -> Normals:
    """Mean t_mean per metro and day of year from rows dated before `cutoff` only, then a
    circular moving mean over 2 * half_window + 1 days."""
    past = obs[pd.to_datetime(obs["date"]) < pd.Timestamp(cutoff)]
    if past.empty:
        raise ValueError(f"no observations before {cutoff}")
    wide = (
        past.assign(doy=day_of_year(past["date"]))
        .pivot_table(index="doy", columns="cbsa_code", values="t_mean", aggfunc="mean")
        .reindex(range(1, 366))
    )
    v = wide.to_numpy()
    padded = np.concatenate([v[-half_window:], v, v[:half_window]]) if half_window else v
    win = np.lib.stride_tricks.sliding_window_view(padded, 2 * half_window + 1, axis=0)
    smooth = pd.DataFrame(np.nanmean(win, axis=-1), index=wide.index, columns=wide.columns)
    years = past.assign(y=pd.to_datetime(past["date"]).dt.year).groupby("cbsa_code")["y"].nunique()
    return Normals(smooth, years, cutoff)


def anomalies(obs: pd.DataFrame, normals: Normals) -> pd.DataFrame:
    """Daily t_mean minus the metro's normal, one row per calendar day, one column per metro."""
    wide = obs.pivot_table(index="date", columns="cbsa_code", values="t_mean", aggfunc="mean")
    wide.index = pd.DatetimeIndex(wide.index)
    wide = wide.reindex(pd.date_range(wide.index.min(), wide.index.max(), freq="D"))
    norm = normals.table.reindex(columns=wide.columns).loc[day_of_year(wide.index)].to_numpy()
    return wide - norm


def forward_targets(closes: pd.DataFrame) -> pd.DataFrame:
    """Return from close t to close t+h for the primary, and the primary minus each benchmark."""
    c = closes.sort_index()
    out = {}
    for h in HS:
        r = c.shift(-h) / c - 1
        out[f"{key(PRIMARY)}_{h}d"] = r[PRIMARY]
        for b in BENCHMARKS:
            out[f"xs_{key(b)}_{h}d"] = r[PRIMARY] - r[b]
    return pd.DataFrame(out, index=c.index)[list(TARGETS)]


def lagged(anom: pd.DataFrame, sessions: pd.DatetimeIndex, lag: int, kind: str) -> pd.DataFrame:
    """The anomaly (or its one-day change) on the calendar day `lag` days before each session."""
    base = anom if kind == "level" else anom.diff()
    days = pd.DatetimeIndex(sessions) - pd.Timedelta(days=lag)
    return base.reindex(days).set_axis(pd.DatetimeIndex(sessions))


@dataclass(frozen=True)
class Panel:
    sessions: pd.DatetimeIndex
    metros: list[str]
    features: dict[tuple[str, int], np.ndarray]  # (kind, lag) -> rows x metros
    targets: np.ndarray  # rows x len(TARGETS)


def panel(anom: pd.DataFrame, targets: pd.DataFrame, start: date, end: date) -> Panel:
    """Sessions in [start, end] where every feature and every target is present."""
    t = targets.loc[pd.Timestamp(start) : pd.Timestamp(end)]
    t.index = pd.DatetimeIndex(t.index)
    metros = sorted(anom.columns)
    feats = {(k, lag): lagged(anom[metros], t.index, lag, k) for k in KINDS for lag in LAGS}
    ok = t.notna().all(axis=1).to_numpy().copy()
    for f in feats.values():
        ok &= f.notna().all(axis=1).to_numpy()
    return Panel(
        t.index[ok],
        metros,
        {key: f.to_numpy()[ok] for key, f in feats.items()},
        t.to_numpy()[ok],
    )
