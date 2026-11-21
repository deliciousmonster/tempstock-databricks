"""Alpha Vantage: one claimed slot an hour, at most 24 calls a UTC day, spaced from `sent_at`.

Slot `22` is the first benchmark and `23` the second, after the US close; every other slot is the
primary, unless a benchmark is owed, in which case that slot carries it. A claim is a create-if-absent file; a result file
records what the slot did, `sent_at` null when nothing was sent.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Protocol

from weather_signal.config import BENCHMARKS, PRIMARY
from weather_signal.ingest.files import Outcome, Unreadable
from weather_signal.sessions import Calendar

OWN_SLOT = dict(zip(BENCHMARKS, (22, 23), strict=True))
SLOT_TICKER = {slot: t for t, slot in OWN_SLOT.items()}
CARRY_ORDER = BENCHMARKS
MAX_CARRIES = 3
CLAIM_WINDOW = timedelta(minutes=5)
DAY = timedelta(days=1)

CallOutcome = Literal[
    "landed", "not_landed", "invalid", "rate_note", "timeout", "unreachable", "not_sent"
]
UNKNOWN: dict[str, Any] = {"unknown": True}


class SlotRefused(ValueError):
    """A slot name outside 00..23: a 25th claim in a UTC day."""


class Store(Protocol):
    def put_if_absent(self, path: str, data: bytes) -> Outcome: ...
    def read(self, path: str) -> bytes | None: ...


@dataclass(frozen=True)
class CallResult:
    outcome: CallOutcome
    newest_bar: date | None = None


@dataclass(frozen=True)
class SlotReport:
    hour: int
    ticker: str | None
    carried: bool
    outcome: str
    sent_at: datetime | None = None
    reason: str = ""


def slot_name(hour: int) -> str:
    if not 0 <= hour <= 23:
        raise SlotRefused(f"slot {hour} does not exist; 24 calls a day at most")
    return f"{hour:02d}"


def slot_start(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


@dataclass
class Ledger:
    root: str
    store: Store
    _cache: dict[str, dict[str, Any] | None] = field(default_factory=dict)

    def path(self, day: date, hour: int, kind: str = "") -> str:
        suffix = ".result.json" if kind == "result" else ".json"
        return f"{self.root}/alphavantage/{day.isoformat()}/{slot_name(hour)}{suffix}"

    def _read(self, path: str) -> dict[str, Any] | None:
        if path not in self._cache:
            try:
                raw = self.store.read(path)
                self._cache[path] = None if raw is None else json.loads(raw)
            except (Unreadable, ValueError):
                self._cache[path] = UNKNOWN
        return self._cache[path]

    def result(self, at: datetime) -> dict[str, Any] | None:
        return self._read(self.path(at.date(), at.hour, "result"))

    def claim_record(self, at: datetime) -> dict[str, Any] | None:
        return self._read(self.path(at.date(), at.hour))

    def claim(self, day: date, hour: int, record: dict[str, Any]) -> Outcome:
        path = self.path(day, hour)
        return self.store.put_if_absent(path, json.dumps(record).encode())

    def write_result(self, day: date, hour: int, record: dict[str, Any]) -> Outcome:
        path = self.path(day, hour, "result")
        self._cache[path] = record
        return self.store.put_if_absent(path, json.dumps(record, default=str).encode())

    def slots(self, start: datetime, end: datetime) -> list[dict[str, Any] | None]:
        """Result records of the slots in [start, end)."""
        out, t = [], start
        while t < end:
            out.append(self.result(t))
            t += timedelta(hours=1)
        return out


# Owed tickers ---------------------------------------------------------------------------------


def own_slot_before(ticker: str, tau: datetime) -> datetime:
    s = slot_start(tau.date(), OWN_SLOT[ticker])
    return s if s <= tau else s - DAY


def due_session(ticker: str, tau: datetime, cal: Calendar) -> date:
    return cal.latest_closed_before(own_slot_before(ticker, tau))


def _landed(rec: dict[str, Any] | None, ticker: str, session: date) -> bool:
    if not rec or rec is UNKNOWN or rec.get("ticker") != ticker or rec.get("outcome") != "landed":
        return False
    bar = rec.get("newest_bar")
    return bar is not None and date.fromisoformat(str(bar)) >= session


def _window(ticker: str, session: date) -> datetime:
    return slot_start(session, OWN_SLOT[ticker])


def owed(ledger: Ledger, ticker: str, tau: datetime, cal: Calendar) -> bool:
    """Owed once its own slot for the due session has passed with no landed bar since."""
    here = slot_start(tau.date(), tau.hour)
    session = due_session(ticker, tau, cal)
    start = _window(ticker, session)
    if start >= here:
        return False
    recs = ledger.slots(start, here)
    if any(_landed(r, ticker, session) for r in recs):
        return False
    carries = sum(1 for r in recs if r and r.get("ticker") == ticker and r.get("carried"))
    return carries < MAX_CARRIES


def _last_carried(ledger: Ledger, tau: datetime) -> str | None:
    here = slot_start(tau.date(), tau.hour)
    for r in reversed(ledger.slots(here - 2 * DAY, here)):
        if r and r.get("carried"):
            return str(r["ticker"])
    return None


def pick(ledger: Ledger, tau: datetime, cal: Calendar) -> tuple[str, bool]:
    """The ticker slot `tau.hour` calls, and whether it is a carried call."""
    base = SLOT_TICKER.get(tau.hour)
    if base is not None:
        session = due_session(base, tau, cal)
        here = slot_start(tau.date(), tau.hour)
        done = any(_landed(r, base, session) for r in ledger.slots(_window(base, session), here))
        return (PRIMARY, False) if done else (base, False)
    due = [t for t in CARRY_ORDER if owed(ledger, t, tau, cal)]
    if not due:
        return PRIMARY, False
    if len(due) == 2 and _last_carried(ledger, tau) == due[0]:
        return due[1], True
    return due[0], True


# Spacing --------------------------------------------------------------------------------------


def spacing_anchor(ledger: Ledger, tau: datetime) -> datetime | None:
    """The instant this slot's previous send can latest have left, or None when nothing was sent."""
    prev = slot_start(tau.date(), tau.hour) - DAY
    res = ledger.result(prev)
    if res and res is not UNKNOWN:
        sent = res.get("sent_at")
        return datetime.fromisoformat(sent) if sent else None
    claim = ledger.claim_record(prev)
    if claim is None:
        return None
    if claim is UNKNOWN:
        return prev + timedelta(hours=1) + CLAIM_WINDOW
    return datetime.fromisoformat(claim["claimed_at"]) + CLAIM_WINDOW


# One slot -------------------------------------------------------------------------------------


@dataclass
class Clock:
    now: Callable[[], datetime]
    sleep: Callable[[float], None]


def run_slot(
    ledger: Ledger, cal: Calendar, clock: Clock, call: Callable[[str], CallResult]
) -> SlotReport:
    tau = clock.now()
    hour = tau.hour
    ticker, carried = pick(ledger, tau, cal)
    claimed_at = clock.now()
    outcome = ledger.claim(
        tau.date(),
        hour,
        {"claimed_at": claimed_at.isoformat(), "ticker": ticker, "carried": carried},
    )
    if outcome != "created":
        return SlotReport(hour, None, False, f"claim_{outcome}")
    anchor = spacing_anchor(ledger, tau)
    rec: dict[str, Any] = {
        "ticker": ticker,
        "carried": carried,
        "claimed_at": claimed_at.isoformat(),
    }
    if anchor is not None:
        send_at = anchor + DAY
        if send_at - claimed_at > CLAIM_WINDOW:
            rec |= {"sent_at": None, "outcome": "not_sent", "reason": "spacing"}
            ledger.write_result(tau.date(), hour, rec)
            return SlotReport(hour, ticker, carried, "not_sent", None, "spacing")
        wait = (send_at - clock.now()).total_seconds()
        if wait > 0:
            clock.sleep(wait)
    sent_at = clock.now()
    result = call(ticker)
    rec |= {
        "sent_at": sent_at.isoformat(),
        "outcome": result.outcome,
        "newest_bar": result.newest_bar.isoformat() if result.newest_bar else None,
    }
    ledger.write_result(tau.date(), hour, rec)
    return SlotReport(hour, ticker, carried, result.outcome, sent_at)
