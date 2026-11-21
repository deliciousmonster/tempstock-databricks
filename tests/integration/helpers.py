"""Scratch tables, frames and the agent's MERGE for the integration tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pandas as pd

from weather_signal.catalog.ddl import TableSpec, specs
from weather_signal.config import Names
from weather_signal.sessions import load as load_calendar
from weather_signal.signal import score
from weather_signal.signal.features import Tables
from weather_signal.signal.model import AlwaysHold

AGENT_MERGE = """
MERGE INTO {recs} r
USING (
  SELECT s.as_of, s.target_date, s.horizon_days, s.action AS signal_action,
         CASE WHEN :downgrade THEN 'hold' ELSE s.action END AS action,
         s.confidence AS confidence, :rationale AS rationale,
         CASE WHEN :downgrade AND s.action <> 'hold' THEN :downgrade_reason END AS downgrade_reason,
         :llm_model_id AS llm_model_id,
         concat('models:/', s.model_name, '/', s.model_version) AS signal_model_uri,
         :trace_id AS trace_id, :agent_version AS agent_version, current_timestamp() AS written_at,
         :llm_route AS llm_route
  FROM {signal} s WHERE s.as_of = :as_of AND s.target_date = :target_date
) src
ON r.as_of = src.as_of AND r.target_date = src.target_date
WHEN NOT MATCHED THEN INSERT *
"""


def agent_args(as_of: datetime, target, downgrade: bool = False) -> dict:
    return {
        "downgrade": downgrade,
        "llm_route": "none",
        "rationale": "it",
        "downgrade_reason": "it",
        "llm_model_id": "it",
        "trace_id": "it",
        "agent_version": "it",
        "as_of": as_of,
        "target_date": target,
    }


def scratch_as_of(run_id: str) -> datetime:
    """A unique `:30` in 2014 (inside the calendar) per run; the append-only tables keep every test's rows."""
    h = int(hashlib.sha256(run_id.encode()).hexdigest(), 16) % (364 * 24)
    return datetime(2014, 1, 1, 0, 30, tzinfo=UTC) + timedelta(hours=h)


def frame(as_of: datetime) -> list[dict]:
    empty_fc = pd.DataFrame(columns=["target_date", "lead_days", "anomaly_c", "known_at"])
    empty_px = pd.DataFrame(columns=["ticker", "trade_date", "close", "known_at"])
    return score.frame(as_of, 30, Tables(empty_fc, empty_px), load_calendar(), AlwaysHold(), as_of)


def scratch_spec(n: Names, base: str, run_id: str) -> TableSpec:
    s = specs(n)[n.table("signals", base)]
    return TableSpec(f"{s.name}_it_{run_id}", s.columns, s.key, s.constraints, s.cluster_by)


def drop_scratch(spark, n: Names, name: str) -> None:
    spark.sql(f"DROP TABLE IF EXISTS {name}").collect()
    for t in ("guard_state", "guard_acts"):
        spark.sql(f"DELETE FROM {n.schema('ops')}.{t} WHERE table_name = '{name}'").collect()
