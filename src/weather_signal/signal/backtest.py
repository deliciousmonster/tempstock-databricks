"""`signals.signal_backtest`: each primary session from `START` scored at horizon 0 with what was known.

A session's decision sits at `asof.backtest_as_of`. The forecast feature is the archived lead the
live frame would read; where none is visible, the latest observed ERA5 anomaly known by then,
and a hold (`stale_input`) when that day is older than `asof.MAX_OBSERVED_AGE`. Each row carries
its position, the return it is held on and its net P&L, and every row of a run the run's gate.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from weather_signal import asof
from weather_signal.config import PRIMARY
from weather_signal.sessions import Calendar
from weather_signal.signal import backtest_fit, gate, score
from weather_signal.signal.features import Features, Tables, build
from weather_signal.signal.model import (
    FORECAST_FEATURE,
    BaseSignalModel,
    Signal,
    SignalInput,
    ThresholdModel,
)

# The seventh session of 1991, the first with a five-day return on the primary's 1991-01-02 bar.
START = date(1991, 1, 10)
HORIZON = 0
LEAD = asof.fitted_lead(HORIZON)
PRICE_WINDOW = timedelta(days=21)
FORECAST, OBSERVED, NO_INPUT = "forecast", "observed", "none"
# Why a session with no usable anomaly holds; a forecast keeps the model's own reason.
MISSING = {NO_INPUT: "no_input", OBSERVED: "no_normal"}
# An observed day past `asof.MAX_OBSERVED_AGE` holds; only the newest session's fails the run.
STALE_INPUT = "stale_input"
NEWEST_STALE = "newest_stale"
MAX_MISSING_SHARE = 0.01


@dataclass(frozen=True)
class Walk:
    """A run's rows, its gate, the latest refit (which the score deploys) and its held days."""

    rows: list[dict[str, Any]]
    verdict: gate.Gate
    latest: backtest_fit.Refit | None
    days: list[gate.Day]


class TooManyMissing(RuntimeError):
    """More sessions held for a missing input than a sound run has; an input table is short."""


@dataclass(frozen=True)
class Inputs:
    """The whole history the run reads; every row carries its `known_at`."""

    forecast: pd.DataFrame
    prices: pd.DataFrame
    observed: pd.DataFrame


def run_id(model: BaseSignalModel) -> str:
    return f"h{HORIZON}:{model.model_name}:{model.model_version}"


def sessions(prices: pd.DataFrame, now: datetime, start: date = START) -> list[date]:
    """Primary trade dates from `start` whose decision instant has passed."""
    days = {pd.Timestamp(d).date() for d in prices.loc[prices["ticker"] == PRIMARY, "trade_date"]}
    return sorted(d for d in days if d >= start and asof.backtest_as_of(d) <= now)


class _Index:
    """The inputs sorted once, so each session slices them instead of scanning the history."""

    def __init__(self, inp: Inputs) -> None:
        px = inp.prices.assign(_d=pd.to_datetime(inp.prices["trade_date"])).sort_values("_d")
        self.px, self.px_days = px.drop(columns="_d"), px["_d"].to_numpy()
        fc = inp.forecast[inp.forecast["lead_days"] == LEAD]
        self.fc = dict(iter(fc.groupby(pd.to_datetime(fc["target_date"]).dt.date)))
        self.fc_empty = fc.iloc[0:0]
        self.obs = inp.observed.sort_values("date").reset_index(drop=True)
        known = pd.to_datetime(self.obs["known_at"], utc=True)
        self.obs_known = known.dt.tz_localize(None).to_numpy()

    def tables(self, day: date) -> Tables:
        # Trimming by date only shortens the search; `build` still filters on known_at.
        lo, hi = (np.datetime64(d, "ns") for d in (day - PRICE_WINDOW, day))
        i, j = self.px_days.searchsorted(lo), self.px_days.searchsorted(hi, side="right")
        return Tables(self.fc.get(day, self.fc_empty), self.px.iloc[i:j])

    def use_observed(self, f: Features, as_of: datetime) -> bool:
        """Put the latest observed anomaly known at `as_of` in the forecast feature's place."""
        limit = pd.Timestamp(asof.utc(as_of)).tz_localize(None).to_datetime64()
        seen = np.flatnonzero(self.obs_known <= limit)
        if not len(seen):
            return False
        last = self.obs.iloc[seen[-1]]
        f.values[FORECAST_FEATURE] = float(last["anomaly_c"])
        f.known_at.append(pd.Timestamp(last["known_at"]).to_pydatetime())
        f.extra |= {"observed_date": str(pd.Timestamp(last["date"]).date())}
        return True


def _features(day: date, idx: _Index, cal: Calendar) -> tuple[Features, str]:
    as_of = asof.backtest_as_of(day)
    f = build(day, day, idx.tables(day), as_of=as_of, cal=cal)
    kind = FORECAST if "forecast_known_at" in f.extra else NO_INPUT
    if kind == NO_INPUT and idx.use_observed(f, as_of):
        kind = OBSERVED
    asof.assert_no_lookahead([{"known_at": k} for k in f.known_at], as_of)
    return f, kind


def _row(
    day: date, f: Features, kind: str, model: BaseSignalModel, now: datetime
) -> dict[str, Any]:
    as_of = asof.backtest_as_of(day)
    sig = model.predict(SignalInput(as_of, day, HORIZON, f.values, f.inputs_known_at or as_of))
    if _stale(day, f, kind):
        sig = _hold(sig, STALE_INPUT)
    elif kind in MISSING and _missing(f.values.get(FORECAST_FEATURE)):
        sig = _hold(sig, MISSING[kind])
    row = score.row(as_of, day, HORIZON, sig, model, f, now)
    return row | {
        "backtest_run_id": run_id(model),
        "input_kind": kind,
        "created_at": now,
    }


def _hold(sig: Signal, reason: str) -> Signal:
    prices = tuple(r for r in sig.reasons if r == "stale_prices")
    return Signal("hold", 0.0, 0.0, (reason, *prices))


def _stale(day: date, f: Features, kind: str) -> bool:
    if kind != OBSERVED:
        return False
    return asof.observed_stale(day, date.fromisoformat(str(f.extra["observed_date"])))


def _feature(day: date, f: Features, kind: str) -> float:
    """The forecast feature a fit may learn from; a stale observation is none."""
    return math.nan if _stale(day, f, kind) else f.values.get(FORECAST_FEATURE, math.nan)


def _missing(x: float | None) -> bool:
    return x is None or not math.isfinite(x)


def summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Rows by `input_kind`, the sessions held for each missing or stale input, and whether the
    newest session (the last scored) held on a stale observation."""
    kinds = Counter(r["input_kind"] for r in rows)
    reasons = {*MISSING.values(), STALE_INPUT}
    held = Counter(r["reasons"][0] for r in rows if r["reasons"] and r["reasons"][0] in reasons)
    return (
        {"rows": len(rows)}
        | {k: kinds[k] for k in (FORECAST, OBSERVED, NO_INPUT)}
        | {m: held[m] for m in sorted(reasons)}
        | {NEWEST_STALE: int(bool(rows) and rows[-1]["reasons"][:1] == [STALE_INPUT])}
    )


def check_missing(s: dict[str, int]) -> None:
    """A past gap in ERA5 only holds; a feed that stopped holds the newest session and fails."""
    if s[NEWEST_STALE]:
        raise TooManyMissing(f"the newest session held on a stale observed input: {s}")
    held = sum(s[m] for m in set(MISSING.values()))
    if held > MAX_MISSING_SHARE * s["rows"]:
        raise TooManyMissing(f"{held} of {s['rows']} sessions held for a missing input: {s}")


def rows(
    days: Iterable[date],
    inp: Inputs,
    cal: Calendar,
    model: BaseSignalModel | None,
    now: datetime,
) -> list[dict[str, Any]]:
    """Score `days` in order; with no `model`, `backtest_fit` refits one each calendar year."""
    return _walk(days, inp, cal, model, now).rows


def walk(days: Iterable[date], inp: Inputs, cal: Calendar, now: datetime) -> Walk:
    """The yearly walk-forward run the job writes and whose latest refit the train job deploys."""
    return _walk(days, inp, cal, None, now)


def _walk(
    days: Iterable[date],
    inp: Inputs,
    cal: Calendar,
    model: BaseSignalModel | None,
    now: datetime,
) -> Walk:
    idx, labels = _Index(inp), backtest_fit.labels(inp.prices)
    seen: list[backtest_fit.Example] = []
    out: list[dict[str, Any]] = []
    year, m, latest = None, model or backtest_fit.fit([], now), None
    for day in days:
        f, kind = _features(day, idx, cal)
        if model is None and day.year != year:
            latest = backtest_fit.refit(seen, asof.backtest_as_of(day))
            year, m = day.year, latest.model
        if isinstance(m, ThresholdModel):
            f.extra |= {"model_coef": m.coef, "model_threshold_c": m.threshold}
        out.append(_row(day, f, kind, m, now))
        x = _feature(day, f, kind)
        if not math.isnan(x) and day in labels:
            seen.append(backtest_fit.Example(x, *labels[day]))
    return _with_pnl(out, labels, latest)


def _with_pnl(
    out: list[dict[str, Any]],
    labels: dict[date, tuple[float, datetime]],
    latest: backtest_fit.Refit | None,
) -> Walk:
    """Each row's position, return and net P&L, then the run's gate on every row."""
    held = []
    for r in out:
        pos, label = gate.position(r["action"]), labels.get(r["target_date"])
        ret = label[0] if label else None
        r |= {"pos": pos, "ret": ret, "pnl": None if ret is None else gate.pnl(pos, ret)}
        if ret is not None:
            held.append(gate.Day(r["target_date"], pos, ret))
    g = gate.judge(held)
    for r in out:
        r |= {"gate_total": g.total, "gate_p95": g.p95, "gate_pass": g.passed}
        r |= {"gate_n": g.n, "gate_trades": g.trades}
    return Walk(out, g, latest, held)


def examples(days: Iterable[date], inp: Inputs, cal: Calendar) -> list[backtest_fit.Example]:
    """Each session's forecast feature with its next-session label, as `rows` collects them."""
    idx, labels = _Index(inp), backtest_fit.labels(inp.prices)
    out = []
    for day in days:
        x = _feature(day, *_features(day, idx, cal))
        if not math.isnan(x) and day in labels:
            out.append(backtest_fit.Example(x, *labels[day]))
    return out
