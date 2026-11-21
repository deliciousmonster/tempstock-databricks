"""The `app.v_ui_*` views and `app.fn_ui_*` functions every UI surface reads.

`selects(n)` returns each view's body so the spark tests can build it over fixtures; `statements(n)`
is what `setup` runs. JSON fields are read with `get_json_object`, which local Spark also parses.
"""

from __future__ import annotations

from weather_signal.catalog.ddl import BACKTEST_VIEW as BACKTEST_VIEW
from weather_signal.catalog.ddl import backtest_view
from weather_signal.config import PRIMARY, Names
from weather_signal.signal import checks
from weather_signal.signal.model import STOCK_RET_5D

VIEWS = (
    "v_ui_calls",
    "v_ui_outlook",
    "v_ui_status",
    "v_ui_index",
    "v_ui_agent_runs",
    "v_ui_sources",
)

# Read by the App alone; setup makes it beside its table. The gate, Genie and the snapshot keep to VIEWS.
backtest = backtest_view
# Display only, like the backtest view outside VIEWS: the snapshot and the dashboard read it, no model.
INTRADAY_VIEW = "v_ui_intraday"


def intraday(n: Names) -> str:
    """Recent hourly closes, the last 5 sessions `daily.prices_intraday` keeps."""
    return (
        "SELECT i.ticker, i.bar_start, to_date(i.bar_start) AS session_date, "
        "s.close_utc AS session_close, i.close, i.volume, i.final, i.fetched_at "
        f"FROM {n.table('daily', 'prices_intraday')} i LEFT JOIN {n.table('daily', 'sessions')} s "
        "ON s.session_date = to_date(i.bar_start)"
    )


CALL_COLUMNS = (
    "as_of TIMESTAMP, target_date DATE, horizon_days INT, signal_action STRING, "
    "signal_confidence DOUBLE, forecast_anomaly_c DOUBLE, last_close DOUBLE, agent_action STRING, "
    "shown_action STRING, downgrade_reason STRING, rationale STRING, audit_problem STRING, "
    "run_outcome STRING, written_at TIMESTAMP, source STRING"
)
CALL_SELECT = (
    "as_of, target_date, horizon_days, signal_action, signal_confidence, forecast_anomaly_c, "
    "last_close, agent_action, shown_action, downgrade_reason, rationale, audit_problem, "
    "run_outcome, written_at, source"
)


def _calls(n: Names) -> str:
    signals = n.schema("signals")
    return f"""
WITH audit AS (
  SELECT as_of, target_date,
         concat_ws(',', array_sort(array_distinct(flatten(array(
           collect_set(problem),
           IF(max(copies) > 1, array('duplicate'), CAST(array() AS ARRAY<STRING>))))))) AS problems
  FROM {signals}.v_recommendation_audit GROUP BY as_of, target_date),
runs AS (
  SELECT as_of, max_by(outcome, started_at) AS run_outcome FROM {signals}.agent_runs GROUP BY as_of)
SELECT coalesce(s.as_of, r.as_of) AS as_of,
       coalesce(s.target_date, r.target_date) AS target_date,
       coalesce(s.horizon_days, r.horizon_days) AS horizon_days,
       s.action AS signal_action, s.score, s.confidence AS signal_confidence, s.reasons,
       s.model_name, s.model_version, s.inputs_max_known_at, s.ticker,
       CAST(get_json_object(s.features, '$.forecast_anomaly_c') AS DOUBLE) AS forecast_anomaly_c,
       CAST(get_json_object(s.features, '$.forecast_known_at') AS TIMESTAMP) AS forecast_known_at,
       CAST(get_json_object(s.features, '$.last_close') AS DOUBLE) AS last_close,
       CAST(get_json_object(s.features, '$.{STOCK_RET_5D}') AS DOUBLE) AS ret_5d,
       s.est_price, s.est_reason, s.est_basis,
       r.action AS agent_action, r.downgrade_reason, r.rationale, r.trace_id,
       r.llm_model_id, r.agent_version, r.written_at,
       CASE WHEN r.as_of IS NULL THEN 'signal_only' ELSE 'agent' END AS source,
       r.action AS shown_action,
       nullif(a.problems, '') AS audit_problem,
       u.run_outcome
FROM {signals}.signal s
FULL OUTER JOIN {signals}.recommendations r ON r.as_of = s.as_of AND r.target_date = s.target_date
LEFT JOIN audit a ON a.as_of = coalesce(s.as_of, r.as_of)
  AND a.target_date = coalesce(s.target_date, r.target_date)
LEFT JOIN runs u ON u.as_of = coalesce(s.as_of, r.as_of)"""


def _outlook(n: Names) -> str:
    signals, app = n.schema("signals"), n.schema("app")
    return (
        f"SELECT c.* FROM {app}.v_ui_calls c "
        f"WHERE c.as_of = (SELECT max(as_of) FROM {signals}.signal)"
    )


def _status(n: Names) -> str:
    # The gate is the newest backtest run's, repeated on each of its rows.
    signals = n.schema("signals")
    return f"""
WITH gate AS (
  SELECT backtest_run_id, gate_pass, gate_total, gate_p95, gate_n, gate_trades
  FROM {signals}.signal_backtest
  WHERE backtest_run_id = (SELECT max_by(backtest_run_id, created_at) FROM {signals}.signal_backtest)
  LIMIT 1)
SELECT (SELECT max(as_of) FROM {signals}.signal) AS latest_signal_as_of,
       (SELECT max(as_of) FROM {signals}.recommendations) AS latest_agent_as_of,
       g.open_incident, g.post_verified_as_of, g.tables AS guarded_tables,
       (SELECT count(*) FROM {signals}.v_recommendation_audit) AS audit_problems,
       (SELECT max_by(outcome, started_at) FROM {signals}.agent_runs) AS last_run_outcome,
       (SELECT max_by(route, started_at) FROM {signals}.agent_runs) AS last_run_route,
       (SELECT max(finished_at) FROM {signals}.agent_runs) AS last_run_finished_at,
       current_timestamp() AS read_at,
       b.backtest_run_id AS gate_run_id, b.gate_pass, b.gate_total, b.gate_p95, b.gate_n,
       b.gate_trades
FROM {signals}.v_guard_hold g LEFT JOIN gate b ON true"""


def _index(n: Names) -> str:
    # The spine is every index day and every primary close; neither drops the other.
    signals, daily = n.schema("signals"), n.schema("daily")
    return f"""
WITH t AS (SELECT date, index_c, n_metros FROM {signals}.temp_index_daily),
p AS (SELECT trade_date AS date, ticker, close, ret FROM {daily}.prices_daily
      WHERE ticker = '{PRIMARY}')
SELECT coalesce(t.date, p.date) AS date, t.index_c, nm.normal_c,
       t.index_c - nm.normal_c AS anomaly_c, t.n_metros, p.ticker, p.close, p.ret
FROM t FULL OUTER JOIN p ON p.date = t.date
LEFT JOIN {signals}.normals nm ON nm.day_of_year = dayofyear(coalesce(t.date, p.date))"""


def _agent_runs(n: Names) -> str:
    return (
        "SELECT run_id, as_of, started_at, finished_at, route, outcome, detail, "
        "(unix_timestamp(finished_at) - unix_timestamp(started_at)) AS duration_s "
        f"FROM {n.table('signals', 'agent_runs')}"
    )


def _sources(n: Names) -> str:
    signals, daily, raw, ops = (n.schema(s) for s in ("signals", "daily", "raw", "ops"))
    window = checks.FILL_ALERT_WINDOW.days
    return f"""
WITH env AS (
  SELECT source, max(fetched_at) AS last_fetched_at,
         count_if(fetched_at > current_timestamp() - INTERVAL 24 HOURS) AS landed_24h,
         count_if(NOT coalesce(sha_ok, false) OR coalesce(http_status, 0) <> 200) AS bad_kept
  FROM {raw}.raw_envelopes GROUP BY source),
fresh AS (
  SELECT 'daily' AS layer, 'forecast_hourly' AS dataset, max(known_at) AS at FROM {daily}.forecast_hourly
  UNION ALL SELECT 'daily', 'prices_daily', max(known_at) FROM {daily}.prices_daily
  UNION ALL SELECT 'signals', 'forecast_index', max(known_at) FROM {signals}.forecast_index
  UNION ALL SELECT 'signals', 'temp_index_daily', CAST(max(date) AS TIMESTAMP) FROM {signals}.temp_index_daily),
upd AS (
  SELECT max_by(origin.update_id, timestamp) AS update_id FROM {ops}.pipeline_events
  WHERE event_type = 'update_progress'),
upd_state AS (
  SELECT max_by(get_json_object(details, '$.update_progress.state'), timestamp) AS state,
         max(timestamp) AS at
  FROM {ops}.pipeline_events
  WHERE event_type = 'update_progress' AND origin.update_id = (SELECT update_id FROM upd)),
per_exp AS (
  SELECT element_at(split(e.dataset, '[.]'), -1) AS dataset, e.name,
         sum(e.passed_records) AS passed, sum(e.failed_records) AS failed
  FROM {ops}.pipeline_events p
  LATERAL VIEW explode(from_json(get_json_object(p.details, '$.flow_progress.data_quality.expectations'),
    'array<struct<name:string,dataset:string,passed_records:bigint,failed_records:bigint>>')) t AS e
  WHERE p.event_type = 'flow_progress' AND p.origin.update_id = (SELECT update_id FROM upd)
  GROUP BY 1, 2),
per_ds AS (
  SELECT dataset, max(passed + failed) AS rows_checked, sum(failed) AS violations,
         count(*) AS expectations FROM per_exp GROUP BY dataset),
fill_file AS (
  SELECT path, count(DISTINCT cbsa_code, lead_days) AS cells, count(DISTINCT cbsa_code) AS metros
  FROM {daily}.forecast_fill_quarantine
  WHERE fetched_at > current_timestamp() - INTERVAL {window} DAYS GROUP BY path),
fill_shape AS (
  SELECT coalesce(max_by(cells, cells * 1000 + metros), 0) AS cells,
         coalesce(max_by(metros, cells * 1000 + metros), 0) AS metros FROM fill_file)
SELECT 'source' AS kind, 'raw' AS layer, source AS name, last_fetched_at AS at,
       landed_24h AS n, bad_kept AS n_bad, CAST(NULL AS STRING) AS state FROM env
UNION ALL SELECT 'fresh', layer, dataset, at, NULL, NULL, NULL FROM fresh
UNION ALL SELECT 'update', 'pipeline', 'last_update', at, NULL, NULL, state FROM upd_state
UNION ALL SELECT 'expectations', CASE WHEN d.dataset = 'raw_envelopes' THEN 'raw' ELSE 'daily' END,
       d.dataset, u.at, d.rows_checked, d.violations, u.state
FROM per_ds d CROSS JOIN upd_state u
UNION ALL SELECT 'fill_shape', 'daily', 'forecast_fill_quarantine', NULL, cells, metros, NULL
FROM fill_shape"""


def selects(n: Names) -> dict[str, str]:
    """Each view's body by name, in creation order (`v_ui_outlook` reads `v_ui_calls`)."""
    return {
        "v_ui_calls": _calls(n),
        "v_ui_outlook": _outlook(n),
        "v_ui_status": _status(n),
        "v_ui_index": _index(n),
        "v_ui_agent_runs": _agent_runs(n),
        "v_ui_sources": _sources(n),
    }


def functions(n: Names) -> list[str]:
    """The five UC functions the Claude beat reads; each is one SELECT over one view."""
    app = n.schema("app")
    return [
        f"CREATE OR REPLACE FUNCTION {app}.fn_ui_outlook() RETURNS TABLE ({CALL_COLUMNS}) "
        "COMMENT 'The latest outlook: one row per target date. shown_action is the agent call; "
        "null means no agent call, and signal_action is the model signal, not a call.' "
        f"RETURN SELECT {CALL_SELECT} FROM {app}.v_ui_outlook",
        f"CREATE OR REPLACE FUNCTION {app}.fn_ui_calls(target_date DATE) "
        f"RETURNS TABLE ({CALL_COLUMNS}) "
        "COMMENT 'Every call made for one target date, each as_of and horizon.' "
        f"RETURN SELECT {CALL_SELECT} FROM {app}.v_ui_calls c "
        "WHERE c.target_date = fn_ui_calls.target_date",
        f"CREATE OR REPLACE FUNCTION {app}.fn_ui_runs(hours INT) RETURNS TABLE "
        "(run_id STRING, as_of TIMESTAMP, started_at TIMESTAMP, finished_at TIMESTAMP, "
        "route STRING, outcome STRING, detail STRING, duration_s BIGINT) "
        "COMMENT 'Agent runs started in the last N hours.' "
        f"RETURN SELECT * FROM {app}.v_ui_agent_runs r "
        "WHERE r.started_at >= current_timestamp() - make_interval(0, 0, 0, 0, fn_ui_runs.hours)",
        f"CREATE OR REPLACE FUNCTION {app}.fn_ui_sources() RETURNS TABLE "
        "(kind STRING, layer STRING, name STRING, at TIMESTAMP, n BIGINT, n_bad BIGINT, state STRING) "
        "COMMENT 'Sources, freshness and the last pipeline update with its expectations.' "
        f"RETURN SELECT * FROM {app}.v_ui_sources WHERE kind <> 'fill_shape'",
        f"CREATE OR REPLACE FUNCTION {app}.fn_ui_status() RETURNS TABLE "
        "(open_incident BOOLEAN, post_verified_as_of TIMESTAMP, guarded_tables BIGINT, "
        "read_at TIMESTAMP) "
        "COMMENT 'Guard inputs for the integrity rule; they are inputs, not the integrity state.' "
        "RETURN SELECT open_incident, post_verified_as_of, guarded_tables, read_at "
        f"FROM {app}.v_ui_status",
    ]


def statements(n: Names) -> list[str]:
    app = n.schema("app")
    views = [f"CREATE OR REPLACE VIEW {app}.{k} AS {v.strip()}" for k, v in selects(n).items()]
    views.append(f"CREATE OR REPLACE VIEW {app}.{INTRADAY_VIEW} AS {intraday(n)}")
    return views + functions(n)
