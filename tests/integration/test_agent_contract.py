"""The agent's contract through Connect as the agent principal, profile `weather-signal-agent`."""

import configparser
from pathlib import Path

import pytest

from tests.integration.conftest import sql_error

AGENT_PROFILE = "weather-signal-agent"
FUNCTIONS = {
    "fn_agent_inputs": [
        "target_date",
        "horizon_days",
        "signal_action",
        "score",
        "signal_confidence",
        "reasons",
        "model_name",
        "model_version",
        "forecast_anomaly_c",
        "forecast_known_at",
        "last_close",
        "last_close_date",
        "ret_5d",
        "inputs_max_known_at",
    ],
    "fn_forecast_index": [
        "target_date",
        "lead_days",
        "index_c",
        "normal_c",
        "anomaly_c",
        "known_at",
    ],
}


def _has_profile() -> bool:
    cfg = configparser.ConfigParser()
    cfg.read(Path.home() / ".databrickscfg")
    return cfg.has_section(AGENT_PROFILE)


@pytest.fixture(scope="module")
def agent():
    if not _has_profile():
        pytest.skip(
            f"no {AGENT_PROFILE} profile: the agent principal has no OAuth secret on this Mac"
        )
    from databricks.connect import DatabricksSession

    return DatabricksSession.builder.profile(AGENT_PROFILE).serverless().getOrCreate()


def test_function_columns(agent, n):
    signals = n.schema("signals")
    for fn, cols in FUNCTIONS.items():
        df = agent.sql(f"SELECT * FROM {signals}.{fn}(TIMESTAMP'2001-01-01 00:30')")
        assert df.columns == cols
    assert agent.sql(f"SELECT * FROM {signals}.v_guard_hold").columns[:2] == [
        "open_incident",
        "post_verified_as_of",
    ]


def test_refused(agent, n):
    signals, ops = n.schema("signals"), n.schema("ops")
    for q in (
        f"INSERT INTO {signals}.signal (as_of) VALUES (now())",
        f"ALTER TABLE {signals}.signal SET TBLPROPERTIES ('x' = 'y')",
        f"UPDATE {ops}.guard_state SET open_incident = false",
        f"SELECT * FROM {ops}.guard_state",
    ):
        assert "PERMISSION_DENIED" in sql_error(agent, q) or "INSUFFICIENT" in sql_error(
            agent, q
        ), q
