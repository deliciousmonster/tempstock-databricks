"""The job-owned tables, their constraints and properties, and the reconcile `setup` runs as a guard act."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from weather_signal.config import Names

RETENTION = "interval 30 days"
REQUIRED_PROPERTIES = {
    "delta.appendOnly": "true",
    "delta.enableChangeDataFeed": "true",
    "delta.deletedFileRetentionDuration": RETENTION,
    "delta.logRetentionDuration": RETENTION,
}
RETENTION_KEYS = ("delta.deletedFileRetentionDuration", "delta.logRetentionDuration")
MIN_RETENTION_DAYS = 30


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: str
    key: tuple[str, ...]
    constraints: dict[str, str] = field(default_factory=dict)
    cluster_by: tuple[str, ...] = ()
    guarded: bool = True

    def create_sql(self) -> str:
        props = ", ".join(f"'{k}' = '{v}'" for k, v in REQUIRED_PROPERTIES.items())
        cluster = f" CLUSTER BY ({', '.join(self.cluster_by)})" if self.cluster_by else ""
        return f"CREATE TABLE IF NOT EXISTS {self.name} ({self.columns}){cluster} TBLPROPERTIES ({props})"


SCORED_COLUMNS = (
    "as_of TIMESTAMP NOT NULL, target_date DATE NOT NULL, horizon_days INT NOT NULL, "
    "action STRING NOT NULL, score DOUBLE, confidence DOUBLE NOT NULL, reasons ARRAY<STRING>, "
    "model_name STRING, model_version STRING, features STRING, inputs_max_known_at TIMESTAMP, "
    "scored_at TIMESTAMP NOT NULL, ticker STRING NOT NULL"
)
# Columns added after a table's first create go last: `setup` adds them, and rows are positional.
EST_COLUMNS = "est_price DOUBLE, est_reason STRING, est_basis STRING"
SIGNAL_COLUMNS = f"{SCORED_COLUMNS}, {EST_COLUMNS}"
PNL_COLUMNS = (
    "pos INT, ret DOUBLE, pnl DOUBLE, gate_total DOUBLE, gate_p95 DOUBLE, gate_pass BOOLEAN, "
    "gate_n INT, gate_trades INT"
)
BACKTEST_COLUMNS = (
    f"{SCORED_COLUMNS}, backtest_run_id STRING NOT NULL, input_kind STRING NOT NULL, "
    f"created_at TIMESTAMP NOT NULL, {PNL_COLUMNS}"
)
RECOMMENDATION_COLUMNS = (
    "as_of TIMESTAMP NOT NULL, target_date DATE NOT NULL, horizon_days INT, signal_action STRING, "
    "action STRING, confidence DOUBLE, rationale STRING, downgrade_reason STRING, "
    "llm_model_id STRING, signal_model_uri STRING, trace_id STRING, agent_version STRING, "
    "written_at TIMESTAMP, llm_route STRING"
)
AGENT_RUNS_COLUMNS = (
    "run_id STRING NOT NULL, as_of TIMESTAMP NOT NULL, started_at TIMESTAMP, finished_at TIMESTAMP, "
    "outcome STRING NOT NULL, route STRING NOT NULL, fallback_reason STRING, "
    "databricks_service STRING, api_error_status INT, refusal STRING, error STRING, writer STRING, "
    "rows_written INT, downgrades INT, hook_denials INT, backend STRING, cli_version STRING, "
    "api_provider STRING, subscription STRING, llm_model_id STRING, sdk_version STRING, "
    "duration_ms INT, duration_api_ms INT, num_turns INT, cost_usd_equiv DOUBLE, prompt_sha STRING, "
    "agent_version STRING, trace_id STRING, detail STRING"
)
LLM_ROUTES = ("databricks-gateway", "direct", "direct_fallback", "none")
AGENT_OUTCOMES = (
    "ok",
    "partial",
    "llm_unavailable",
    "llm_incomplete",
    "auth_refused",
    "signal_missing",
    "connect_failed",
    "late_start",
    "deadline_killed",
    "crashed",
    "guard_hold",
)


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def specs(n: Names) -> dict[str, TableSpec]:
    signal = n.table("signals", "signal")
    recs = n.table("signals", "recommendations")
    return {
        signal: TableSpec(
            signal,
            SIGNAL_COLUMNS,
            ("as_of", "target_date"),
            {
                "action_valid": "action IN ('buy', 'sell', 'hold')",
                "confidence_range": "confidence BETWEEN 0 AND 1",
            },
            ("as_of",),
        ),
        recs: TableSpec(
            recs,
            RECOMMENDATION_COLUMNS,
            ("as_of", "target_date"),
            {
                "never_flip": "action IN (signal_action, 'hold')",
                "action_not_null": "action IS NOT NULL",
                "confidence_not_null": "confidence IS NOT NULL",
                # Rows from before change 12 have none; repair reads a NULL CHECK as false
                "llm_route_valid": f"llm_route IS NULL OR {_in('llm_route', LLM_ROUTES)}",
            },
            ("as_of",),
        ),
    }


def agent_runs(n: Names) -> TableSpec:
    """The agent's run log; created here by the owner so the agent principal needs no CREATE TABLE."""
    return TableSpec(
        n.table("signals", "agent_runs"),
        AGENT_RUNS_COLUMNS,
        ("run_id",),
        {
            "outcome_valid": _in("outcome", AGENT_OUTCOMES),
            "route_valid": _in("route", LLM_ROUTES),
            "fallback_has_reason": "route <> 'direct_fallback' OR fallback_reason IS NOT NULL",
        },
        guarded=False,
    )


def recommendations_eval(n: Names) -> TableSpec:
    """The eval's MERGE target (`--eval-run`): the recommendation columns plus `eval_run_id`."""
    return TableSpec(
        n.table("signals", "recommendations_eval"),
        f"{RECOMMENDATION_COLUMNS}, eval_run_id STRING NOT NULL",
        ("eval_run_id", "as_of", "target_date"),
        {"never_flip": "action IN (signal_action, 'hold')"},
        guarded=False,
    )


def signal_backtest(n: Names) -> str:
    """Rebuilt per run id by the `backtest` task, so neither append-only nor guarded."""
    return (
        f"CREATE TABLE IF NOT EXISTS {n.table('signals', 'signal_backtest')} ({BACKTEST_COLUMNS})"
    )


BACKTEST_VIEW = "v_ui_backtest"


def backtest_view(n: Names) -> str:
    """The newest run's rows with the session's close and that year's fit, for the App alone.

    Not in the UI's `VIEWS`, so the SQL gate, Genie and the snapshot never read it.
    """
    signals, daily = n.schema("signals"), n.schema("daily")
    return f"""
SELECT b.backtest_run_id, b.as_of, b.target_date, b.action, b.score, b.confidence, b.reasons,
       b.model_name, b.model_version, b.input_kind,
       CAST(get_json_object(b.features, '$.forecast_anomaly_c') AS DOUBLE) AS anomaly_c,
       CAST(get_json_object(b.features, '$.observed_date') AS DATE) AS observed_date,
       CAST(get_json_object(b.features, '$.model_coef') AS DOUBLE) AS model_coef,
       CAST(get_json_object(b.features, '$.model_threshold_c') AS DOUBLE) AS model_threshold_c,
       b.inputs_max_known_at, b.ticker, p.close AS close, b.created_at,
       b.pos, b.ret, b.pnl, b.gate_total, b.gate_p95, b.gate_pass, b.gate_n, b.gate_trades
FROM {signals}.signal_backtest b
LEFT JOIN {daily}.prices_daily p ON p.ticker = b.ticker AND p.trade_date = b.target_date
WHERE b.backtest_run_id = (SELECT max_by(backtest_run_id, created_at) FROM {signals}.signal_backtest)"""


def norm(expr: str) -> str:
    return re.sub(r"\s+", "", expr).lower()


def retention_days(value: str | None) -> float:
    m = re.fullmatch(r"\s*interval\s+(\d+)\s+(day|days|hour|hours)\s*", value or "", re.I)
    if not m:
        return 0.0
    n = float(m.group(1))
    return n if m.group(2).lower().startswith("day") else n / 24


@dataclass(frozen=True)
class Planned:
    operation: str
    parameters: dict[str, str]
    sql: str


def add_columns_sql(table: str, columns: str, have: Sequence[str]) -> str | None:
    """The ALTER that appends the columns `have` lacks, in spec order; None when none are missing."""
    present = {c.lower() for c in have}
    missing = [c for c in columns.split(", ") if c.split()[0].lower() not in present]
    if not missing:
        return None
    return f"ALTER TABLE {table} ADD COLUMNS ({', '.join(missing)})"


def reconcile_plan(
    spec: TableSpec, props: dict[str, str], *, columns: Sequence[str] | None = None
) -> list[Planned]:
    """The commits that bring `props` (from SHOW TBLPROPERTIES) and `columns` to the spec, in order."""
    plan: list[Planned] = []
    for name, expr in spec.constraints.items():
        have = props.get(f"delta.constraints.{name}")
        if have is not None and norm(have) == norm(expr):
            continue
        if have is not None:
            plan.append(
                Planned(
                    "DROP CONSTRAINT",
                    {"constraint": name},
                    f"ALTER TABLE {spec.name} DROP CONSTRAINT {name}",
                )
            )
        plan.append(
            Planned(
                "ADD CONSTRAINT",
                {"name": name, "expr": expr},
                f"ALTER TABLE {spec.name} ADD CONSTRAINT {name} CHECK ({expr})",
            )
        )
    missing = {k: v for k, v in REQUIRED_PROPERTIES.items() if not _prop_ok(k, props.get(k))}
    if missing:
        body = ", ".join(f"'{k}' = '{v}'" for k, v in missing.items())
        plan.append(
            Planned(
                "SET TBLPROPERTIES",
                {"properties": json.dumps(missing, separators=(",", ":"))},
                f"ALTER TABLE {spec.name} SET TBLPROPERTIES ({body})",
            )
        )
    add = None if columns is None else add_columns_sql(spec.name, spec.columns, columns)
    if add:
        # Delta records the columns as JSON; the act matches on the operation and user alone.
        plan.append(Planned("ADD COLUMNS", {}, add))
    return plan


def _prop_ok(key: str, have: str | None) -> bool:
    if key in RETENTION_KEYS:
        return retention_days(have) >= MIN_RETENTION_DAYS
    return have == REQUIRED_PROPERTIES[key]


def state_problems(spec: TableSpec, props: dict[str, str], filters_or_masks: int) -> list[str]:
    """Step 2 of the guard: what in the table's current state differs from the spec."""
    out = [f"{k} is {props.get(k)!r}" for k in REQUIRED_PROPERTIES if not _prop_ok(k, props.get(k))]
    for name, expr in spec.constraints.items():
        have = props.get(f"delta.constraints.{name}")
        if have is None:
            out.append(f"constraint {name} missing")
        elif norm(have) != norm(expr):
            out.append(f"constraint {name} is {have!r}")
    if filters_or_masks:
        out.append(f"{filters_or_masks} row filters or column masks")
    return out
