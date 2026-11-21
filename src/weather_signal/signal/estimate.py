"""The estimated price beside a buy or sell call: display only, never a model input.

The last WMT close visible at the call, compounded once per exchange session up to the target day
by the champion walk's mean return on that side net of costs (owner, 2026-11-20):

    move      = mean_gross[side] - pos * COST     (buy: mean - cost; sell: mean + cost)
    est_price = close * (1 + move) ** sessions

No estimate, with a reason instead: no champion, a hold, a basis from another fit, under
`MIN_TRADES` days on the side, a net move against the call, or no close. Reason names follow
the reference's `estimate.py`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from weather_signal.sessions import Calendar
from weather_signal.signal.gate import COST, POSITION, Side

MIN_TRADES = 30
NO_CHAMPION, HOLD, NO_BASIS = "no_champion", "hold", "no_basis"
FEW_TRADES, NO_EDGE, NO_CLOSE = "few_trades", "no_edge_after_costs", "no_close"


@dataclass(frozen=True)
class Basis:
    """The walk's per-side returns, tied to the refit they were measured for."""

    fit_as_of: str
    sides: Mapping[str, Side]
    cost: float = COST

    def to_json(self) -> str:
        sides = {k: {"mean_gross": s.mean_gross, "n": s.n} for k, s in self.sides.items()}
        body = {"fit_as_of": self.fit_as_of, "cost": self.cost, **sides}
        return json.dumps(body, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> Basis:
        d = json.loads(text)
        sides = {k: Side(d[k]["mean_gross"], int(d[k]["n"])) for k in POSITION}
        return cls(str(d["fit_as_of"]), sides, float(d["cost"]))


@dataclass(frozen=True)
class Estimate:
    price: float | None
    reason: str | None
    basis: dict[str, Any] = field(default_factory=dict)

    def columns(self) -> dict[str, Any]:
        """`est_price`, `est_reason` and `est_basis`, in the signal table's order."""
        text = json.dumps(self.basis, sort_keys=True) if self.basis else None
        return {"est_price": self.price, "est_reason": self.reason, "est_basis": text}


def sessions_after(cal: Calendar, day: date, through: date) -> int:
    """Exchange sessions in (day, through]."""
    return len(cal.between(day + timedelta(days=1), through)) if through > day else 0


def estimate(
    action: str,
    *,
    fit_as_of: str | None,
    basis: Basis | None,
    close: tuple[float, date] | None,
    target: date,
    cal: Calendar,
) -> Estimate:
    """`fit_as_of` is the champion's, None with no champion; `close` is the last visible one."""
    if fit_as_of is None:
        return Estimate(None, NO_CHAMPION)
    if action not in POSITION:
        return Estimate(None, HOLD)
    if basis is None or basis.fit_as_of != fit_as_of:
        return Estimate(None, NO_BASIS)
    side, pos = basis.sides.get(action), POSITION[action]
    if side is None or side.mean_gross is None or side.n < MIN_TRADES:
        return Estimate(None, FEW_TRADES)
    move = round(side.mean_gross - pos * basis.cost, 8)
    if move * pos <= 0:
        return Estimate(None, NO_EDGE)
    if close is None:
        return Estimate(None, NO_CLOSE)
    price, day = close
    n = sessions_after(cal, day, target)
    why = {"close": price, "close_date": day.isoformat(), "side": action, "sessions": n}
    why |= {"mean_gross": side.mean_gross, "cost": basis.cost, "move": move}
    why |= {"n_trades": side.n, "fit_as_of": fit_as_of}
    return Estimate(round(price * (1 + move) ** n, 4), None, why)
