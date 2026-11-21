"""The run's own MLflow span, whose id every row carries; any MLflow failure costs the trace, never the rows.

Start and end each run in a thread joined with a bound, so a slow tracking server can't hold the hour.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

EXPERIMENT = "/Shared/weather-signal-agent"
JOIN_S = 20.0


def tracking_uri(profile: str) -> str:
    if not profile:
        raise ValueError(
            "a profile is required; bare `databricks` would sign as the default profile"
        )
    return f"databricks://{profile}"


def bounded(fn: Callable[[], Any], seconds: float, name: str) -> Any:
    """`fn()` in a daemon thread; its result, or `TimeoutError` once `seconds` pass."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["ok"] = fn()
        except Exception as err:  # noqa: BLE001 - handed to the caller, never raised in the thread
            box["err"] = err

    t = threading.Thread(target=run, name=name, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise TimeoutError(f"{name} still running after {seconds:.0f} s")
    if "err" in box:
        raise box["err"]
    return box.get("ok")


class Tracer:
    def __init__(self, uri: str, *, experiment: str = EXPERIMENT, join_s: float = JOIN_S):
        self.uri = uri
        self.experiment = experiment
        self.join_s = join_s
        self.span: Any = None

    def start(self, run_id: str, attributes: dict[str, str]) -> str:
        """The span's trace id, or `untraced:<run_id>` when MLflow fails or overruns."""
        try:
            self.span = bounded(lambda: self._start(attributes), self.join_s, "mlflow-start")
            return str(self.span.trace_id)
        except Exception as err:  # noqa: BLE001 - tracing is optional by design
            log.warning("tracing off for this run: %s", type(err).__name__)
            return f"untraced:{run_id}"

    def _start(self, attributes: dict[str, str]) -> Any:
        import mlflow

        mlflow.set_tracking_uri(self.uri)
        mlflow.set_experiment(self.experiment)
        try:
            mlflow.anthropic.autolog(silent=True)
        except Exception as err:  # noqa: BLE001 - autolog is a bonus; the span is the record
            log.info("autolog unavailable: %s", type(err).__name__)
        return mlflow.start_span_no_context("hourly_run", attributes=attributes)

    def finish(self, attributes: dict[str, str], outputs: dict[str, Any]) -> None:
        if self.span is None:
            return
        span = self.span

        def end() -> None:
            import mlflow

            span.set_attributes(attributes)
            span.set_outputs(outputs)
            span.end()
            mlflow.flush_trace_async_logging()  # os._exit would drop the queue

        try:
            bounded(end, self.join_s, "mlflow-upload")
        except Exception as err:  # noqa: BLE001 - the rows already carry the span's id
            log.warning("trace upload failed: %s", type(err).__name__)
