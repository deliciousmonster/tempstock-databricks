"""The XNYS session calendar from the packaged seed/sessions.csv (scripts/build_sessions.py)."""

from __future__ import annotations

import bisect
import csv
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import cache
from importlib import resources
from pathlib import Path

COMPLETE_AFTER = timedelta(minutes=15)


@dataclass(frozen=True)
class Session:
    day: date
    open: datetime
    close: datetime


class Calendar:
    def __init__(self, sessions: list[Session], end: date) -> None:
        self.sessions = sorted(sessions, key=lambda s: s.day)
        self.days = [s.day for s in self.sessions]
        self.by_day = {s.day: s for s in self.sessions}
        self.end = end

    def is_session(self, day: date) -> bool:
        return day in self.by_day

    def close(self, day: date) -> datetime:
        return self.by_day[day].close

    def previous(self, day: date) -> date:
        """The session strictly before `day`."""
        i = bisect.bisect_left(self.days, day)
        if i == 0:
            raise LookupError(f"no session before {day}")
        return self.days[i - 1]

    def latest_closed_before(self, instant: datetime, margin: timedelta = COMPLETE_AFTER) -> date:
        """The latest session whose close plus `margin` precedes `instant`."""
        i = bisect.bisect_right(self.days, instant.astimezone(UTC).date())
        while i > 0:
            s = self.sessions[i - 1]
            if s.close + margin < instant:
                return s.day
            i -= 1
        raise LookupError(f"no session closed before {instant}")

    def between(self, start: date, end: date) -> list[date]:
        lo = bisect.bisect_left(self.days, start)
        hi = bisect.bisect_right(self.days, end)
        return self.days[lo:hi]


def _parse(row: dict[str, str]) -> Session:
    return Session(
        day=date.fromisoformat(row["session_date"]),
        open=datetime.fromisoformat(row["open_utc"]).replace(tzinfo=UTC),
        close=datetime.fromisoformat(row["close_utc"]).replace(tzinfo=UTC),
    )


def load(path: Path | None = None) -> Calendar:
    if path is None:
        return _packaged()
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return Calendar([_parse(r) for r in rows], end=date.fromisoformat(rows[0]["end_date"]))


@cache
def _packaged() -> Calendar:
    ref = resources.files("weather_signal").joinpath("seed/sessions.csv")
    with resources.as_file(ref) as p:
        return load(Path(p))
