"""Load `src/weather_signal_etl/transformations/pipeline.py` against a recording `dlt` (needs an active Spark session)."""

from __future__ import annotations

import builtins
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

PIPELINE = Path(__file__).parents[2] / "src/weather_signal_etl/transformations/pipeline.py"
CONF = {"weather_signal.catalog": "c", "weather_signal.p_hours": "30", "weather_signal.raw": "/r"}


class _Recorder(types.ModuleType):
    """Every `dlt.<kind>(...)` is recorded; decorators attach their expectations to the table."""

    def __init__(self) -> None:
        super().__init__("dlt")
        self.pending: list[tuple[str, str, str]] = []
        self.tables: dict[str, list[tuple[str, str, str]]] = {}

    def __getattr__(self, kind: str) -> Any:
        if kind.startswith("__"):
            raise AttributeError(kind)
        return lambda *a, **k: self._call(kind, a, k)

    def _call(self, kind: str, a: tuple, k: dict) -> Any:
        if kind.startswith("expect"):
            pairs = a[0].items() if isinstance(a[0], dict) else [(a[0], a[1])]
            exp = [(kind, n, c) for n, c in pairs]
            return lambda f: (self.pending.extend(exp), f)[1]
        if kind in ("table", "view") and a and callable(a[0]):
            return self._close(a[0].__name__, a[0])
        if kind in ("table", "view"):
            return lambda f: self._close(k.get("name", f.__name__), f)
        if kind == "create_streaming_table":
            exps = [(e, n, c) for e, d in k.items() if e.startswith("expect") for n, c in d.items()]
            self.tables[k["name"].rsplit(".", 1)[-1]] = exps
        return None

    def _close(self, name: str, f: Any) -> Any:
        self.tables[name.rsplit(".", 1)[-1]] = self.pending
        self.pending = []
        return f


def load() -> dict[str, list[tuple[str, str, str]]]:
    """Table name (unqualified) to its (kind, expectation, condition) list."""
    rec = _Recorder()
    conf = types.SimpleNamespace(get=lambda key, default=None: CONF.get(key, default))
    spec = importlib.util.spec_from_file_location("weather_signal_pipeline", PIPELINE)
    mod = importlib.util.module_from_spec(spec)
    saved = sys.modules.get("dlt")
    sys.modules["dlt"] = rec
    builtins.spark = types.SimpleNamespace(conf=conf)  # type: ignore[attr-defined]
    try:
        spec.loader.exec_module(mod)
    finally:
        del builtins.spark  # type: ignore[attr-defined]
        if saved is None:
            sys.modules.pop("dlt")
        else:
            sys.modules["dlt"] = saved
    return rec.tables
