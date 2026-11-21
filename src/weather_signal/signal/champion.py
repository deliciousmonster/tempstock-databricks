"""The deployed champion: the walk's latest refit, trading only targets inside its period.

A registered version carries the refit's identity and its gate in tags. The score trades it for
targets on or before `period_through` (31 December of the refit year) and holds every other target
with `no_champion`; a version without a period never trades.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime

from weather_signal.signal import backtest_fit
from weather_signal.signal.estimate import Basis
from weather_signal.signal.gate import Gate
from weather_signal.signal.model import (
    AlwaysHold,
    BaseSignalModel,
    Signal,
    SignalInput,
    ThresholdModel,
    from_params,
)

NO_CHAMPION = "no_champion"


def as_of_tag(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class NoChampion(AlwaysHold):
    """No `@champion` alias: every target holds and says why."""

    def decide(self, x: SignalInput) -> Signal:
        return Signal("hold", 0.0, 1.0, (NO_CHAMPION,))


class Deployed(BaseSignalModel):
    """A registered refit, its period and the estimate basis measured with it."""

    def __init__(
        self,
        model: BaseSignalModel,
        *,
        period_through: date | None,
        fit_as_of: str | None,
        basis: Basis | None,
    ) -> None:
        self.model, self.period_through = model, period_through
        self.fit_as_of, self.basis = fit_as_of, basis
        self.model_name, self.model_version = model.model_name, model.model_version

    def predict(self, x: SignalInput) -> Signal:
        if self.period_through is None or x.target_date > self.period_through:
            return Signal("hold", 0.0, 0.0, (NO_CHAMPION,))
        return self.model.predict(x)


def tags(refit: backtest_fit.Refit, verdict: Gate, basis: Basis) -> dict[str, str]:
    """The version's tags: the refit, its gate, and the estimate basis, all as strings."""
    m = refit.model
    params = {"kind": "threshold", "coef": m.coef, "threshold": m.threshold}
    return {
        "params": json.dumps(params, sort_keys=True, separators=(",", ":")),
        "fit": f"{backtest_fit.NAME} v{backtest_fit.VERSION}",
        "fit_as_of": as_of_tag(refit.fit_as_of),
        "threshold": str(m.threshold),
        "coef": str(m.coef),
        "n_train": str(refit.n_train),
        "period_through": refit.period_through.isoformat(),
        "gate_pass": str(verdict.passed).lower(),
        "gate_total": repr(verdict.total),
        "gate_p95": "" if verdict.p95 is None else repr(verdict.p95),
        "gate_n": str(verdict.n),
        "gate_trades": str(verdict.trades),
        "est_basis": basis.to_json(),
    }


def from_tags(tags: Mapping[str, str], name: str, version: str) -> Deployed:
    """A version without a period or a basis still loads; it then holds or gives no estimate."""
    model = from_params(json.loads(tags.get("params", "{}")), name, version)
    through = tags.get("period_through")
    basis = tags.get("est_basis")
    return Deployed(
        model if isinstance(model, ThresholdModel) else NoChampion(),
        period_through=date.fromisoformat(through) if through else None,
        fit_as_of=tags.get("fit_as_of"),
        basis=Basis.from_json(basis) if basis else None,
    )
