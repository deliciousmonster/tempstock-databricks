"""The pipeline over fixture envelopes, in its own throwaway schema set `dev_pt<run>_`.

A forged price envelope (fetched before its session's close) and a filled forecast envelope each
fail the update; first sightings de-duplicate across envelopes. The pipeline is created from the
`dev` deployment's file and wheel, and deleted with its schemas afterwards.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.conftest import fixture_bytes
from tests.integration.conftest import PROFILE
from weather_signal import config
from weather_signal.deploy import wheel
from weather_signal.ingest import envelope
from weather_signal.ingest.fetch import RawResponse
from weather_signal.ingest.files import Files
from weather_signal.jobs.score import metrics

ROOT = "/Workspace/Users/{user}/.bundle/weather-signal/dev"
WHEEL = "weather_signal-0.1.0-py3-none-any.whl"
SCHEMAS = ("raw", "daily", "signals", "ops")


def _land(
    files: Files,
    raw: str,
    source: str,
    endpoint: str,
    params: dict,
    body: bytes,
    fetched_at: datetime,
    slot: str,
) -> None:
    env = envelope.build(
        source, endpoint, params, slot, RawResponse(200, {}, body, fetched_at), host="h"
    )
    key = envelope.landing_key(source, endpoint, params, slot)
    assert (
        files.put_if_absent(
            envelope.landing_path(raw, source, fetched_at, key), envelope.encode(env)
        )
        == "created"
    )


@pytest.fixture
def ws():
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient(profile=PROFILE)


def _update(ws, pid: str, timeout_s: int = 1500) -> str:
    uid = ws.pipelines.start_update(pid, full_refresh=True).update_id
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = ws.pipelines.get_update(pid, uid).update.state.value
        if st in ("COMPLETED", "FAILED", "CANCELED"):
            return st
        time.sleep(20)
    return "TIMEOUT"


def _events(spark, ops: str) -> list[dict]:
    rows = spark.sql(
        f"SELECT event_type, message, details FROM {ops}.pipeline_events "
        "WHERE event_type IN ('flow_progress', 'update_progress')"
    ).collect()
    return [r.asDict() for r in rows]


def _pipeline(ws, spark, run_id: str, raw: str, prefix: str) -> str:
    from databricks.sdk.service import pipelines as sp

    user = ws.current_user.me().user_name
    root = ROOT.format(user=user)
    p = ws.pipelines.create(
        name=f"dev_pt{run_id}",
        catalog="workspace",
        schema=f"{prefix}daily",
        serverless=True,
        continuous=False,
        development=True,
        libraries=[
            sp.PipelineLibrary(
                file=sp.FileLibrary(
                    f"{root}/files/src/weather_signal_etl/transformations/pipeline.py"
                )
            )
        ],
        environment=sp.PipelinesEnvironment(
            # dev's wheel as bootstrap deployed it from this tree
            [f"{root}/artifacts/{wheel.tag(Path(__file__).parents[2])}/.internal/{WHEEL}"]
        ),
        configuration={
            "weather_signal.catalog": "workspace",
            "weather_signal.schema_prefix": prefix,
            "weather_signal.p_hours": "30",
            "weather_signal.raw": raw,
        },
        event_log=sp.EventLogSpec(
            catalog="workspace", schema=f"{prefix}ops", name="pipeline_events"
        ),
    )
    return p.pipeline_id


def _setup(spark, ws, run_id, items):
    prefix = f"dev_pt{run_id}_"
    for s in SCHEMAS:
        spark.sql(f"CREATE SCHEMA IF NOT EXISTS workspace.{prefix}{s}").collect()
    spark.sql(f"CREATE VOLUME IF NOT EXISTS workspace.{prefix}raw.raw").collect()
    raw = f"/Volumes/workspace/{prefix}raw/raw"
    files = Files.from_sdk(PROFILE)
    for it in items:
        _land(files, raw, *it)
    return prefix, raw, _pipeline(ws, spark, run_id, raw, prefix)


def _teardown(spark, ws, prefix, pid):
    if pid:
        ws.pipelines.delete(pid)
    for s in SCHEMAS:
        spark.sql(f"DROP SCHEMA IF EXISTS workspace.{prefix}{s} CASCADE").collect()


def _prevruns(name, t, slot):
    from weather_signal.ingest import sources

    params = sources.previous_runs_params(sources.metros())
    return ("open-meteo-previous-runs", sources.PREVIOUS_RUNS, params, fixture_bytes(name), t, slot)


def _av(t, slot):
    return (
        "alphavantage",
        "https://www.alphavantage.co/query",
        {"function": "TIME_SERIES_DAILY", "symbol": "SPY", "outputsize": "compact"},
        fixture_bytes("av-spy-compact.json"),
        t,
        slot,
    )


def _items(case: str) -> list:
    t = datetime(2026, 11, 17, 18, 35, tzinfo=UTC)
    items = [
        _prevruns("prevruns-1807.json", datetime(2026, 11, 17, 18, 7, tzinfo=UTC), "a"),
        _prevruns("prevruns-1835.json", t, "b"),
        _av(datetime(2026, 11, 17, 18, 8, tzinfo=UTC), "c"),
    ]
    if case == "forged_price":
        # The 11-16 bar fetched at 20:50 on 11-16, ten minutes before its session closed.
        items.append(_av(datetime(2026, 11, 16, 20, 50, tzinfo=UTC), "d"))
    if case == "filled_forecast":
        # The filled body is the only Previous Runs envelope: a clean earlier one's first
        # sightings would stand in forecast_hourly, and their days in forecast_daily.
        body = json.loads(fixture_bytes("prevruns-1835.json"))
        h = body["hourly"]
        h["temperature_2m_previous_day3"] = list(h["temperature_2m"])
        src, endpoint, params = _prevruns("prevruns-1835.json", t, "b")[:3]
        items = [(src, endpoint, params, json.dumps(body).encode(), t, "b"), items[2]]
    return items


def _assert_clean(spark, prefix: str, state: str, n_items: int) -> None:
    assert state == "COMPLETED", _events(spark, f"workspace.{prefix}ops")[-3:]
    fh = f"workspace.{prefix}daily.forecast_hourly"
    keys = "cbsa_code, valid_time, lead_days"
    n_rows = spark.sql(f"SELECT count(*) AS c FROM {fh}").first()["c"]
    n_keys = spark.sql(f"SELECT count(DISTINCT {keys}) AS c FROM {fh}").first()["c"]
    assert n_rows == n_keys > 0
    raw = f"workspace.{prefix}raw.raw_envelopes"
    assert spark.sql(f"SELECT count(*) AS c FROM {raw}").first()["c"] == n_items


def _assert_quarantined(spark, prefix: str, state: str) -> None:
    """The filled lead 3 completes the update, is quarantined, and leaves no daily row."""
    assert state == "COMPLETED", _events(spark, f"workspace.{prefix}ops")[-3:]
    daily = f"workspace.{prefix}daily"
    q = spark.sql(f"SELECT DISTINCT lead_days, reason FROM {daily}.forecast_fill_quarantine")
    assert (3, "lead_equals_lead0_6h") in {(r["lead_days"], r["reason"]) for r in q.collect()}
    hourly = spark.sql(f"SELECT count(*) AS c FROM {daily}.forecast_hourly WHERE lead_days = 3")
    daily = spark.sql(f"SELECT count(*) AS c FROM {daily}.forecast_daily WHERE lead_days = 3")
    assert hourly.first()["c"] >= 24 and daily.first()["c"] == 0
    s = spark.sql(f"SELECT max(n_metros) AS n FROM {daily}.forecast_fill_summary")
    assert s.first()["n"] == 1
    # The check task over Connect: naive `collect()` timestamps, one metro is not a fill.
    found = metrics(spark, config.Names("workspace", prefix), datetime.now(UTC).date())
    assert "forecast_filled" not in {f.name for f in found}


@pytest.mark.parametrize("case", ["clean", "forged_price", "filled_forecast"])
def test_pipeline_over_fixtures(spark, ws, run_id, case):
    items = _items(case)
    prefix, pid = f"dev_pt{run_id}_", None
    try:
        prefix, _raw, pid = _setup(spark, ws, run_id, items)
        state = _update(ws, pid)
        if case == "clean":
            _assert_clean(spark, prefix, state, len(items))
        elif case == "filled_forecast":
            _assert_quarantined(spark, prefix, state)
        else:
            assert state == "FAILED"
            text = json.dumps(_events(spark, f"workspace.{prefix}ops"), default=str)
            assert "known_at_bound" in text
    finally:
        _teardown(spark, ws, prefix, pid)
