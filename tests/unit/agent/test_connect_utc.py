"""Connect hands TIMESTAMPs back naive in the host zone; the agent's checks hold in UTC wherever it runs."""

from __future__ import annotations

import os
import sys
import time
import types
from datetime import UTC, datetime

import pytest

from weather_signal import config, spark
from weather_signal.agent import hour
from weather_signal.agent.store import SparkStore

T = pytest.importorskip("pyspark.sql.types")

AS_OF = datetime(2026, 11, 19, 6, 30, tzinfo=UTC)


@pytest.fixture
def denver():
    before = os.environ.get("TZ")
    os.environ["TZ"] = "America/Denver"
    time.tzset()
    yield
    if before is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = before
    time.tzset()


def collected(instant: datetime) -> datetime:
    """`instant` as pyspark's TIMESTAMP conversion hands it to Python (Connect's Arrow path agrees)."""
    return T.TimestampType().fromInternal(int(instant.timestamp()) * 1_000_000)


class _Row:
    def __init__(self, d):
        self.d = d

    def asDict(self):
        return self.d


class _Spark:
    def __init__(self, rows):
        self.rows = rows

    def sql(self, q, args=None):
        return types.SimpleNamespace(collect=lambda: [_Row(r) for r in self.rows(q)])


def _guard(verified, *, incident=False):
    row = {"open_incident": incident, "post_verified_as_of": verified, "tables": 2}
    return SparkStore(_Spark(lambda q: [row]), config.Names("workspace", "dev_"))


def test_the_host_zone_reaches_collected_timestamps(denver):
    assert collected(AS_OF) == datetime(2026, 11, 18, 23, 30)


def test_guard_passes_a_verified_hour_read_west_of_utc(denver):
    assert _guard(collected(AS_OF)).guard(AS_OF) == "ok"


@pytest.mark.parametrize(
    ("verified", "incident", "state"),
    [
        (datetime(2026, 11, 19, 5, 30, tzinfo=UTC), False, "unverified"),
        (AS_OF, True, "incident"),
        (AS_OF, None, "incident"),
        (None, False, "unverified"),
    ],
)
def test_guard_holds_otherwise(denver, verified, incident, state):
    v = verified and collected(verified)
    assert _guard(v, incident=incident).guard(AS_OF) == state


def test_inputs_carry_utc_instants(denver):
    known = collected(datetime(2026, 11, 19, 6, 0, tzinfo=UTC))
    a = {"target_date": "2026-11-20", "inputs_max_known_at": known, "forecast_known_at": known}
    f = {"target_date": "2026-11-20", "known_at": known, "index_c": 1.0}
    store = SparkStore(
        _Spark(lambda q: [a] if "fn_agent_inputs" in q else [f]), config.Names("workspace", "dev_")
    )
    row = store.inputs(AS_OF)["2026-11-20"]
    assert row["forecast_known_at"] == row["inputs_max_known_at"] == "2026-11-19T06:00:00+00:00"


@pytest.fixture
def fake_connect(monkeypatch):
    built = []

    class _Builder:
        def serverless(self):
            return self

        def profile(self, p):
            built.append(p)
            return self

        def getOrCreate(self):
            return "session"

    mod = types.ModuleType("databricks.connect")
    mod.DatabricksSession = types.SimpleNamespace(builder=_Builder())
    if "databricks" not in sys.modules:
        monkeypatch.setitem(sys.modules, "databricks", types.ModuleType("databricks"))
    monkeypatch.setitem(sys.modules, "databricks.connect", mod)
    monkeypatch.delenv("DATABRICKS_RUNTIME_VERSION", raising=False)
    return built


@pytest.mark.parametrize("open_session", [hour.connect_spark, spark.spark_session])
def test_every_connect_session_reads_utc(denver, fake_connect, open_session):
    assert open_session("p") == "session"
    assert fake_connect == ["p"]
    assert collected(AS_OF) == AS_OF.replace(tzinfo=None)


def test_the_eval_opens_connect_through_the_utc_pin(denver, fake_connect, monkeypatch):
    """The eval built its own session and skipped `pin_utc`."""
    from weather_signal.agent import evaluate as run_eval

    spark = types.SimpleNamespace(catalog=types.SimpleNamespace(tableExists=lambda t: False))
    builder = sys.modules["databricks.connect"].DatabricksSession.builder
    monkeypatch.setattr(builder, "getOrCreate", lambda: spark)
    assert run_eval.main([]) == 2  # refuses without the eval table, before any write
    assert fake_connect == ["weather-signal"]
    assert collected(AS_OF) == AS_OF.replace(tzinfo=None)


def test_pin_utc_calls_tzset(monkeypatch):
    """macOS re-reads TZ unasked; other platforms need the call."""
    from weather_signal import spark

    calls = []
    monkeypatch.setenv("TZ", "America/Denver")
    monkeypatch.setattr(spark.time, "tzset", lambda: calls.append(os.environ["TZ"]))
    spark.pin_utc()
    assert calls == ["UTC"]
