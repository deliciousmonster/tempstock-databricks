"""As the agent principal: its own reads and writes succeed, everything else is refused.

Skipped until the owner creates the principal's OAuth secret (docs/agent.md, The agent principal).
"""

import configparser
import os
from pathlib import Path

import pytest

from tests.integration.conftest import sql_error
from weather_signal.config import PRIMARY

PROFILE = os.environ.get("WEATHER_SIGNAL_PROFILE", "weather-signal-agent")


@pytest.fixture(scope="module")
def agent():
    cfg = configparser.ConfigParser()
    cfg.read(Path.home() / ".databrickscfg")
    if PROFILE != "weather-signal-agent" or not cfg.has_section(PROFILE):
        pytest.skip(
            "identity fell back to `weather-signal`: the agent principal has no OAuth secret yet"
        )
    from databricks.connect import DatabricksSession

    return DatabricksSession.builder.profile(PROFILE).serverless().getOrCreate()


def test_principal_reads_writes_and_refusals(agent, n):
    who = agent.sql("SELECT current_user() AS u").first()["u"]
    assert "@" not in who
    signals = n.schema("signals")
    for fn in ("fn_agent_inputs", "fn_forecast_index"):
        agent.sql(f"SELECT * FROM {signals}.{fn}(TIMESTAMP'2001-01-01 00:30')").collect()
    agent.sql(f"SELECT * FROM {signals}.fn_prices('{PRIMARY}', 5)").collect()
    for q in (
        f"INSERT INTO {signals}.signal (as_of, target_date, horizon_days, action, confidence, scored_at) "
        "VALUES (TIMESTAMP'2001-01-01 00:30', DATE'2001-01-01', 0, 'hold', 0.5, current_timestamp())",
        f"UPDATE {n.schema('ops')}.guard_state SET table_name = table_name",
        f"ALTER TABLE {signals}.recommendations UNSET TBLPROPERTIES ('delta.appendOnly')",
    ):
        assert "PERMISSION" in sql_error(agent, q).upper()
