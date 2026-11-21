"""`weather-signal setup`: create or reconcile the job-owned tables as guard acts, then views and functions.

Runs on the Mac as the owner. Inline CHECKs are refused by Delta, so constraints are added by ALTER.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from weather_signal.catalog.ddl import (
    BACKTEST_COLUMNS,
    BACKTEST_VIEW,
    TableSpec,
    add_columns_sql,
    agent_runs,
    backtest_view,
    recommendations_eval,
    reconcile_plan,
    signal_backtest,
    specs,
    state_problems,
)
from weather_signal.config import Names
from weather_signal.guard import check
from weather_signal.guard.acts import latest_or_none, perform
from weather_signal.guard.catalog import Catalog, GuardRow
from weather_signal.guard.repair import RepairRefused, _create_plan
from weather_signal.serving import views as ui_views
from weather_signal.signal.model import STOCK_RET_5D

OPS_DDL = (
    "CREATE TABLE IF NOT EXISTS {ops}.guard_state (table_name STRING, table_id STRING, "
    "checked_version BIGINT, open_incident BOOLEAN, first_bad_version BIGINT, last_bad_version BIGINT, "
    "pre_verified_as_of TIMESTAMP, post_verified_as_of TIMESTAMP, last_notified_at TIMESTAMP, "
    "updated_by STRING)",
    "CREATE TABLE IF NOT EXISTS {ops}.guard_acts (table_name STRING, act_id STRING, table_id STRING, "
    "read_version BIGINT, user_name STRING, planned STRING, status STRING, observed STRING)",
    "CREATE TABLE IF NOT EXISTS {ops}.guard_incidents (table_name STRING, table_id STRING, "
    "first_bad_version BIGINT, last_bad_version BIGINT, kind STRING, commits STRING, "
    "removed_since_good STRING, added_since_good STRING, table_changes STRING, dropped STRING, "
    "recorded_at TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS {ops}.fill_alerts (path STRING, alerted_at TIMESTAMP)",
)


log = logging.getLogger(__name__)
FUNCTIONS = ("fn_agent_inputs", "fn_forecast_index", "fn_prices")
AGENT_TABLES = ("recommendations", "agent_runs")


def reconcile(cat: Catalog, spec: TableSpec) -> str:
    """Create the table or bring it to the spec, as one guard act; refuse on an open incident."""
    row = next((r for r in cat.guard_rows() if r.table_name == spec.name), None)
    if row is not None and row.open_incident:
        raise RepairRefused(f"{spec.name} has an open incident; run guard repair first")
    present = latest_or_none(cat, spec.name) is not None
    if row is not None and (not present or cat.table_id(spec.name) != row.table_id):
        out = check.open_replaced(cat, row, datetime.now(UTC), log.warning)
        raise RepairRefused(f"{out.detail}; run guard repair, or guard adopt-id if deliberate")
    if not present:
        perform(cat, spec.name, _create_plan(spec), read_version=-1)
    plan = reconcile_plan(spec, cat.properties(spec.name), columns=cat.columns(spec.name))
    if plan:
        perform(cat, spec.name, plan)
    problems = state_problems(spec, cat.properties(spec.name), cat.filters_and_masks(spec.name))
    if problems:
        raise RuntimeError(f"{spec.name} after reconcile: {'; '.join(problems)}")
    tid = cat.table_id(spec.name)
    if row is None:
        row = GuardRow(spec.name, tid, cat.latest_version(spec.name))
    row.updated_by = cat.current_user()
    cat.put_guard_row(row)
    return tid


def views_and_functions(n: Names) -> list[str]:
    signals, ops, daily = n.schema("signals"), n.schema("ops"), n.schema("daily")
    return [
        f"CREATE OR REPLACE VIEW {signals}.v_guard_hold AS SELECT "
        "coalesce(max(CAST(open_incident AS INT)), 1) = 1 AS open_incident, "
        "CASE WHEN count(*) = count(post_verified_as_of) THEN min(post_verified_as_of) END "
        "AS post_verified_as_of, count(*) AS tables "
        f"FROM {ops}.guard_state",
        f"CREATE OR REPLACE VIEW {signals}.v_recommendation_audit AS "
        "SELECT r.as_of, r.target_date, r.signal_action, s.action AS scored_action, "
        "count(*) OVER (PARTITION BY r.as_of, r.target_date) AS copies, "
        "CASE WHEN s.action IS NULL THEN 'no_signal' "
        "WHEN r.signal_action <> s.action THEN 'signal_mismatch' "
        "WHEN r.action NOT IN (s.action, 'hold') THEN 'flip' END AS problem "
        f"FROM {signals}.recommendations r LEFT JOIN {signals}.signal s "
        "ON r.as_of = s.as_of AND r.target_date = s.target_date "
        "QUALIFY problem IS NOT NULL OR copies > 1",
        f"CREATE OR REPLACE FUNCTION {signals}.fn_forecast_index(as_of TIMESTAMP) RETURNS TABLE "
        "(target_date DATE, lead_days INT, index_c DOUBLE, normal_c DOUBLE, anomaly_c DOUBLE, "
        "known_at TIMESTAMP) RETURN SELECT target_date, lead_days, index_c, normal_c, "
        f"index_c - normal_c, known_at FROM {signals}.forecast_index f "
        "WHERE f.known_at <= fn_forecast_index.as_of",
        f"CREATE OR REPLACE FUNCTION {signals}.fn_prices(ticker STRING, days INT) RETURNS TABLE "
        "(trade_date DATE, close DOUBLE, known_at TIMESTAMP) RETURN SELECT trade_date, close, known_at "
        f"FROM {daily}.prices_daily p WHERE p.ticker = fn_prices.ticker "
        "AND p.trade_date >= date_sub(current_date(), fn_prices.days)",
        f"CREATE OR REPLACE FUNCTION {signals}.fn_agent_inputs(as_of TIMESTAMP) RETURNS TABLE "
        "(target_date DATE, horizon_days INT, signal_action STRING, score DOUBLE, "
        "signal_confidence DOUBLE, reasons ARRAY<STRING>, model_name STRING, model_version STRING, "
        "forecast_anomaly_c DOUBLE, forecast_known_at TIMESTAMP, last_close DOUBLE, "
        "last_close_date DATE, ret_5d DOUBLE, inputs_max_known_at TIMESTAMP) RETURN "
        "SELECT s.target_date, s.horizon_days, s.action, s.score, s.confidence, s.reasons, "
        "s.model_name, s.model_version, "
        "CAST(get_json_object(s.features, '$.forecast_anomaly_c') AS DOUBLE), "
        "CAST(get_json_object(s.features, '$.forecast_known_at') AS TIMESTAMP), "
        "CAST(get_json_object(s.features, '$.last_close') AS DOUBLE), "
        "CAST(get_json_object(s.features, '$.last_close_date') AS DATE), "
        f"CAST(get_json_object(s.features, '$.{STOCK_RET_5D}') AS DOUBLE), s.inputs_max_known_at "
        f"FROM {signals}.signal s WHERE s.as_of = fn_agent_inputs.as_of",
    ]


def grants(n: Names, runner: str | None, agent: str | None) -> list[str]:
    """Catalog and table grants the bundle cannot express; no `agent` means the one-principal fallback."""
    signals = n.schema("signals")
    out = []
    if runner:
        r = f"`{runner}`"
        out += [
            f"GRANT USE CATALOG ON CATALOG {n.catalog} TO {r}",
            f"GRANT MODIFY ON TABLE {signals}.signal TO {r}",
            f"GRANT MODIFY ON TABLE {signals}.signal_backtest TO {r}",
            *(f"GRANT EXECUTE ON FUNCTION {signals}.{f} TO {r}" for f in FUNCTIONS),
        ]
        if not agent:
            out += [f"GRANT MODIFY ON TABLE {signals}.{t} TO {r}" for t in AGENT_TABLES]
    if agent:
        a = f"`{agent}`"
        out += [
            f"GRANT USE CATALOG ON CATALOG {n.catalog} TO {a}",
            f"GRANT USE SCHEMA, SELECT, EXECUTE ON SCHEMA {signals} TO {a}",
            *(f"GRANT MODIFY ON TABLE {signals}.{t} TO {a}" for t in AGENT_TABLES),
        ]
    return out


def run(cat: Catalog, n: Names) -> dict[str, str]:
    for ddl in OPS_DDL:
        cat.execute(ddl.format(ops=n.schema("ops")))
    ids = {name: reconcile(cat, spec) for name, spec in specs(n).items()}
    for spec in (agent_runs(n), recommendations_eval(n)):
        cat.execute(spec.create_sql())
        for step in reconcile_plan(spec, cat.properties(spec.name)):
            cat.execute(step.sql)
    cat.execute(signal_backtest(n))
    backtest = n.table("signals", "signal_backtest")
    add = add_columns_sql(backtest, BACKTEST_COLUMNS, cat.columns(backtest))
    if add:
        cat.execute(add)
    cat.execute(
        f"CREATE OR REPLACE VIEW {n.schema('app')}.{BACKTEST_VIEW} AS {backtest_view(n).strip()}"
    )
    for sql in views_and_functions(n) + ui_views.statements(n):
        cat.execute(sql)
    return ids
