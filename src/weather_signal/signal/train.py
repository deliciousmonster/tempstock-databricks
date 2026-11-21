"""The champion: the walk's latest refit, registered with its gate, `@champion` only if it passed.

`train` runs the same yearly walk the backtest job writes and registers its latest refit with the
refit's identity, the gate's numbers and the estimate basis as tags. A passing gate sets
`@champion`; a failing one removes it, so the score holds every target with `no_champion` and the
version's `gate_pass=false` tag records why. A newest version whose tags equal the fit's is reused.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from mlflow.pyfunc import PythonModel

from weather_signal import config
from weather_signal.sessions import Calendar
from weather_signal.signal import backtest, backtest_fit, champion, gate
from weather_signal.signal.estimate import Basis
from weather_signal.signal.model import FORECAST_FEATURE, SignalInput, ThresholdModel

log = logging.getLogger(__name__)
# Any instant: the threshold rule's action does not depend on when it is asked.
PROBE_AT = datetime(2026, 1, 1, tzinfo=UTC)
LABEL_LAG = timedelta(hours=24)
MODEL = config.MODEL_NAME
FIT = f"{backtest_fit.NAME} v{backtest_fit.VERSION}"
# What a dev and a prod champion must agree on (`deploy.baseline`).
# Not `gate_pass`: sessions landing after the baseline can move a borderline gate.
COMPARED = ("params", "fit", "fit_as_of", "period_through")
as_of_tag = champion.as_of_tag


class TrainRefused(RuntimeError):
    """The inputs cannot give the walk a refit; nothing is registered."""


class Registry(Protocol):
    def latest(self, name: str) -> tuple[str, Mapping[str, str]] | None: ...
    def register(self, name: str, model: ThresholdModel, tags: Mapping[str, str]) -> str: ...
    def predict(self, name: str, version: str, values: Sequence[float]) -> list[str]: ...
    def set_alias(self, name: str, version: str) -> None: ...
    def drop_alias(self, name: str) -> None: ...


def require_complete(prices: Any, cal: Calendar, as_of: datetime) -> None:
    """The last session closed a label lag before `as_of` must have its close."""
    want = cal.latest_closed_before(as_of - LABEL_LAG)
    days = prices.loc[prices["ticker"] == config.PRIMARY, "trade_date"]
    have = max((d.date() if hasattr(d, "date") else d for d in days), default=None)
    if have is None or have < want:
        raise TrainRefused(
            f"{config.PRIMARY} closes end at {have}; the fit at {as_of_tag(as_of)} needs {want}"
        )


def deployed(
    inp: backtest.Inputs, cal: Calendar, now: datetime
) -> tuple[ThresholdModel, dict[str, str]]:
    """The walk's latest refit and the tags it is registered with."""
    require_complete(inp.prices, cal, now)
    w = backtest.walk(backtest.sessions(inp.prices, now), inp, cal, now)
    if w.latest is None:
        raise TrainRefused("no session to walk; nothing to register")
    basis = Basis(as_of_tag(w.latest.fit_as_of), gate.sides(w.days))
    return w.latest.model, champion.tags(w.latest, w.verdict, basis)


def actions(m: ThresholdModel, values: Sequence[float]) -> list[str]:
    t = PROBE_AT
    return [m.decide(SignalInput(t, t.date(), 0, {FORECAST_FEATURE: v}, t)).action for v in values]


def train(inp: backtest.Inputs, cal: Calendar, reg: Registry, name: str, now: datetime) -> str:
    """The version holding the latest refit, and whether it is the champion."""
    m, want = deployed(inp, cal, now)
    log.info(
        "latest refit %s; gate %s",
        want["fit_as_of"],
        {k: want[k] for k in want if k.startswith("gate")},
    )
    have = reg.latest(name)
    if have is not None and all(have[1].get(k) == v for k, v in want.items()):
        version = have[0]
    else:
        version = reg.register(name, m, want)
    if want["gate_pass"] != "true":
        reg.drop_alias(name)
        return f"{version}, no champion: the gate failed"
    values = [float(v) for v in inp.observed["anomaly_c"].dropna()] or [-3.0, 0.0, 3.0]
    if reg.predict(name, version, values) != actions(m, values):
        raise TrainRefused(
            f"version {version}: the logged model and its tags disagree; no alias set"
        )
    reg.set_alias(name, version)
    return f"{version}, champion"


class MlflowRegistry:
    """Unity Catalog through MLflow, from the job's own workspace identity."""

    def __init__(self, experiment: str) -> None:
        import mlflow

        mlflow.set_tracking_uri("databricks")
        mlflow.set_registry_uri("databricks-uc")
        self.mlflow, self.client, self.experiment = mlflow, mlflow.MlflowClient(), experiment

    def latest(self, name: str) -> tuple[str, Mapping[str, str]] | None:
        return self.newest(self.client, name)

    @staticmethod
    def newest(client: Any, name: str) -> tuple[str, Mapping[str, str]] | None:
        """The highest version and its tags; None when the model has none."""
        try:
            found = client.search_model_versions(f"name='{name}'")
        except Exception as err:
            if getattr(err, "error_code", None) == "RESOURCE_DOES_NOT_EXIST":
                return None
            raise
        if not found:
            return None
        v = str(max(int(mv.version) for mv in found))
        # Unity Catalog's search may leave tags out; the version read carries them.
        return v, dict(client.get_model_version(name, v).tags)

    def register(self, name: str, model: ThresholdModel, tags: Mapping[str, str]) -> str:
        import pandas as pd
        from mlflow.models import infer_signature

        x = pd.DataFrame({FORECAST_FEATURE: [-3.0, 0.0, 3.0]})
        py = Pyfunc(model.coef, model.threshold)
        self.mlflow.set_experiment(self.experiment)
        with self.mlflow.start_run() as run:
            self.mlflow.log_params({"coef": model.coef, "threshold": model.threshold, "fit": FIT})
            info = self.mlflow.pyfunc.log_model(
                artifact_path="model",
                python_model=py,
                signature=infer_signature(x, pd.DataFrame({"action": py.predict(None, x)})),
            )
        noted = {"mlflow_run_id": run.info.run_id, "trained_at": as_of_tag(datetime.now(UTC))}
        mv = self.mlflow.register_model(info.model_uri, name, tags={**tags, **noted})
        return str(mv.version)

    def predict(self, name: str, version: str, values: Sequence[float]) -> list[str]:
        import pandas as pd

        m = self.mlflow.pyfunc.load_model(f"models:/{name}/{version}")
        return [str(a) for a in m.predict(pd.DataFrame({FORECAST_FEATURE: list(values)}))]

    def set_alias(self, name: str, version: str) -> None:
        self.client.set_registered_model_alias(name, "champion", version)

    def drop_alias(self, name: str) -> None:
        try:
            self.client.delete_registered_model_alias(name, "champion")
        except Exception as err:
            if getattr(err, "error_code", None) != "RESOURCE_DOES_NOT_EXIST":
                raise


class Pyfunc(PythonModel):
    """The registered artifact: the same threshold rule over the forecast anomaly column."""

    def __init__(self, coef: float, threshold: float) -> None:
        self.coef, self.threshold = coef, threshold

    def predict(self, context: Any, model_input: Any, params: Any = None) -> list[str]:
        m = ThresholdModel(self.coef, self.threshold)
        return actions(m, [float(v) for v in model_input[FORECAST_FEATURE]])
