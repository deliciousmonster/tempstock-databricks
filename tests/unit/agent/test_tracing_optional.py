"""MLflow failing, hanging or slow costs the trace, never the run; the URI names the worker's profile."""

import re
import sys
import threading
import time
import types

import pytest

from weather_signal.agent import tracing
from weather_signal.agent.tracing import Tracer, tracking_uri

TRACE_ID = "tr-" + "0123456789abcdef" * 2
TRACE = re.compile(r"^tr-[0-9a-f]{32}$")


class FakeSpan:
    """MLflow's LiveSpan carries both ids; `get_trace` answers only the trace id."""

    trace_id = TRACE_ID
    span_id = "a1b2c3d4e5f60718"

    def __init__(self, end=None):
        self._end = end

    def set_attributes(self, _a):
        return None

    def set_outputs(self, _o):
        return None

    def end(self):
        if self._end:
            self._end()


def fake_mlflow(monkeypatch, *, span=None, start_raises=None, start_sleep=0.0):
    m = types.ModuleType("mlflow")
    seen = {}

    def start_span_no_context(name, attributes=None):
        time.sleep(start_sleep)
        if start_raises:
            raise start_raises
        seen["span"] = name
        return span or FakeSpan()

    m.set_tracking_uri = lambda uri: seen.setdefault("uri", uri)
    m.set_experiment = lambda _e: None
    m.anthropic = types.SimpleNamespace(autolog=lambda silent=True: None)
    m.start_span_no_context = start_span_no_context
    m.flush_trace_async_logging = lambda: None
    monkeypatch.setitem(sys.modules, "mlflow", m)
    return seen


def test_uri_is_the_profile():
    assert tracking_uri("weather-signal-agent") == "databricks://weather-signal-agent"
    with pytest.raises(ValueError, match="profile is required"):
        tracking_uri("")


def test_trace_id_not_span_id_on_success(monkeypatch):
    seen = fake_mlflow(monkeypatch)
    t = Tracer(tracking_uri("weather-signal-agent"), join_s=2)
    got = t.start("run1", {"as_of": "x"})
    assert got == TRACE_ID and TRACE.match(got)
    assert seen["uri"] == "databricks://weather-signal-agent"
    t.finish({}, {})


def test_span_raising_gives_untraced(monkeypatch):
    fake_mlflow(monkeypatch, start_raises=RuntimeError("boom"))
    assert Tracer("databricks://p", join_s=2).start("run2", {}) == "untraced:run2"


def test_span_hanging_gives_untraced_within_join(monkeypatch):
    fake_mlflow(monkeypatch, start_sleep=5)
    t0 = time.monotonic()
    assert Tracer("databricks://p", join_s=0.3).start("run3", {}) == "untraced:run3"
    assert time.monotonic() - t0 < 2


def test_upload_raising_or_hanging_returns(monkeypatch):
    block = threading.Event()
    fake_mlflow(monkeypatch, span=FakeSpan(end=block.wait))
    t = Tracer("databricks://p", join_s=0.3)
    assert t.start("run4", {}) == TRACE_ID
    t0 = time.monotonic()
    t.finish({}, {})
    assert time.monotonic() - t0 < 2
    block.set()

    def boom():
        raise OSError("upload")

    fake_mlflow(monkeypatch, span=FakeSpan(end=boom))
    t = Tracer("databricks://p", join_s=2)
    t.start("run5", {})
    t.finish({}, {})


def test_bounded_names_the_thread():
    assert tracing.bounded(lambda: 7, 1, "x") == 7
    with pytest.raises(TimeoutError, match="mlflow-upload"):
        tracing.bounded(lambda: time.sleep(2), 0.1, "mlflow-upload")
