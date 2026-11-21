"""The Open-Meteo budget: a call's weighted cost, the calls sent, and the earliest send under the ceilings."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

MINUTE, HOUR = timedelta(minutes=1), timedelta(hours=1)


def weighted_cost(locations: int, days: int, variables: int) -> float:
    """Open-Meteo's weighted call cost, as its X-RateLimit headers measured it."""
    return locations * max(1.0, days / 14 * variables / 10)


@dataclass(frozen=True)
class Ceilings:
    minute: float = 400
    hour: float = 4000
    day: float = 7000


@dataclass
class Ledger:
    records: list[tuple[datetime, float]] = field(default_factory=list)

    def add(self, at: datetime, cost: float) -> None:
        self.records.append((at, cost))
        self.records.sort()

    def spent(self, start: datetime, end: datetime) -> float:
        return sum(c for t, c in self.records if start < t <= end)

    def day_spent(self, now: datetime) -> float:
        midnight = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return sum(c for t, c in self.records if midnight <= t <= now)

    def _slide(self, now: datetime, window: timedelta, cap: float, cost: float) -> datetime:
        t = now
        for at, _ in self.records:
            if self.spent(t - window, t) + cost <= cap:
                return t
            if at + window > t:
                t = at + window
        return t

    def earliest_send(self, now: datetime, cost: float, caps: Ceilings) -> datetime:
        """The first instant at which sending `cost` stays under every ceiling."""
        t = now
        for _ in range(4):
            t = max(t, self._slide(t, MINUTE, caps.minute, cost))
            t = max(t, self._slide(t, HOUR, caps.hour, cost))
            if self.day_spent(t) + cost > caps.day:
                t = (t.astimezone(UTC) + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
        return t
