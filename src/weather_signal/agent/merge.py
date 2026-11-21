"""The foundation's insert-only MERGE, sourced from `signals.signal`, and the values the agent binds to it.

`llm_route` is bound only when the target table has it; a table setup made before change 12 lacks it.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from weather_signal.agent.proposal import MergeParams

TARGETS = ("recommendations", "recommendations_eval")

_HEAD = """
MERGE INTO {recs} r
USING (
  SELECT s.as_of, s.target_date, s.horizon_days, s.action AS signal_action,
         CASE WHEN :downgrade THEN 'hold' ELSE s.action END AS action,
         s.confidence AS confidence, :rationale AS rationale,
         CASE WHEN :downgrade AND s.action <> 'hold' THEN :downgrade_reason END AS downgrade_reason,
         :llm_model_id AS llm_model_id,
         concat('models:/', s.model_name, '/', s.model_version) AS signal_model_uri,
         :trace_id AS trace_id, :agent_version AS agent_version, current_timestamp() AS written_at"""
_ROUTE = ",\n         :llm_route AS llm_route"
_EVAL = ",\n         :eval_run_id AS eval_run_id"
_TAIL = """
  FROM {signal} s WHERE s.as_of = :as_of AND s.target_date = :target_date
) src
ON r.as_of = src.as_of AND r.target_date = src.target_date{eval_on}
WHEN NOT MATCHED THEN INSERT *
"""


def merge_sql(recs: str, signal: str, *, llm_route: bool, eval_run: bool = False) -> str:
    """The statement for one date; `recs` must end in one of the two allowed table names."""
    if recs.rsplit(".", 1)[-1] not in TARGETS:
        raise ValueError(f"not an allowed MERGE target: {recs}")
    head = _HEAD + (_ROUTE if llm_route else "") + (_EVAL if eval_run else "")
    on = " AND r.eval_run_id = src.eval_run_id" if eval_run else ""
    return (head + _TAIL).format(recs=recs, signal=signal, eval_on=on)


def bind(
    as_of: datetime,
    target_date: date,
    p: MergeParams,
    *,
    meta: dict[str, str],
    llm_route: str | None,
) -> dict[str, Any]:
    """The named markers for one date: never an action, never the in-memory signal."""
    if p.target_date != target_date.isoformat():
        raise ValueError(f"values for {p.target_date} bound to {target_date}")
    out: dict[str, Any] = {
        "as_of": as_of,
        "target_date": target_date,
        "downgrade": p.downgrade,
        "downgrade_reason": p.downgrade_reason,
        "rationale": p.rationale,
        "llm_model_id": meta["llm_model_id"],
        "trace_id": meta["trace_id"],
        "agent_version": meta["agent_version"],
    }
    if llm_route is not None:
        out["llm_route"] = llm_route
    return out
