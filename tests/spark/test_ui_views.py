"""The `v_ui_*` views over fixture rows, each base object a temp view named `<schema>__<object>`."""

from __future__ import annotations

import re
from datetime import date, datetime

import pytest

from weather_signal.config import Names
from weather_signal.serving import views

N = Names("workspace", "dev_")
QUALIFIED = re.compile(r"workspace\.dev_(\w+)\.(\w+)")


def _local(sql: str) -> str:
    return QUALIFIED.sub(r"\1__\2", sql)


def _base(spark, name: str, sql: str) -> None:
    spark.sql(f"CREATE OR REPLACE TEMP VIEW {name} AS {sql}")


def _build(spark) -> None:
    for name, body in views.selects(N).items():
        spark.sql(f"CREATE OR REPLACE TEMP VIEW app__{name} AS {_local(body)}")


@pytest.fixture
def world(spark):
    """An empty world; each test fills the tables it reads."""
    empty = {
        "signals__signal": "SELECT CAST(NULL AS TIMESTAMP) as_of, CAST(NULL AS DATE) target_date, "
        "0 horizon_days, 'hold' action, 0D score, 0.5D confidence, array('x') reasons, "
        "'m' model_name, '1' model_version, CAST(NULL AS STRING) features, "
        "CAST(NULL AS TIMESTAMP) inputs_max_known_at, 'WMT' ticker, CAST(NULL AS DOUBLE) est_price, 'hold' est_reason, CAST(NULL AS STRING) est_basis WHERE false",
        "signals__signal_backtest": "SELECT 'h0:m:1' backtest_run_id, CAST(NULL AS TIMESTAMP) created_at, 0 pos, CAST(NULL AS DOUBLE) ret, CAST(NULL AS DOUBLE) pnl, 0.01D gate_total, 0.02D gate_p95, false gate_pass, 1 gate_n, 0 gate_trades WHERE false",
        "signals__recommendations": "SELECT CAST(NULL AS TIMESTAMP) as_of, CAST(NULL AS DATE) target_date, "
        "0 horizon_days, 'hold' signal_action, 'hold' action, 'r' rationale, "
        "CAST(NULL AS STRING) downgrade_reason, 't' trace_id, 'l' llm_model_id, 'v' agent_version, "
        "CAST(NULL AS TIMESTAMP) written_at WHERE false",
        "signals__v_recommendation_audit": "SELECT CAST(NULL AS TIMESTAMP) as_of, "
        "CAST(NULL AS DATE) target_date, CAST(NULL AS STRING) problem, 1L copies WHERE false",
        "signals__agent_runs": "SELECT 'r' run_id, CAST(NULL AS TIMESTAMP) as_of, "
        "CAST(NULL AS TIMESTAMP) started_at, CAST(NULL AS TIMESTAMP) finished_at, 'x' route, "
        "'ok' outcome, 'd' detail WHERE false",
        "signals__v_guard_hold": "SELECT false open_incident, CAST(NULL AS TIMESTAMP) post_verified_as_of, 2L tables",
        "signals__temp_index_daily": "SELECT CAST(NULL AS DATE) date, 0D index_c, 50L n_metros WHERE false",
        "signals__normals": "SELECT 1 day_of_year, 0D normal_c WHERE false",
        "daily__prices_daily": "SELECT 'WMT' ticker, CAST(NULL AS DATE) trade_date, 0D close, "
        "CAST(NULL AS TIMESTAMP) known_at, 0D ret WHERE false",
        "daily__forecast_hourly": "SELECT CAST(NULL AS TIMESTAMP) known_at WHERE false",
        "daily__forecast_fill_quarantine": "SELECT 'p' path, 'm' cbsa_code, 1 lead_days, "
        "CAST(NULL AS TIMESTAMP) valid_time, CAST(NULL AS TIMESTAMP) fetched_at WHERE false",
        "signals__forecast_index": "SELECT CAST(NULL AS TIMESTAMP) known_at WHERE false",
        "raw__raw_envelopes": "SELECT 'x' source, CAST(NULL AS TIMESTAMP) fetched_at, "
        "true sha_ok, 200 http_status WHERE false",
        "ops__pipeline_events": "SELECT named_struct('update_id', 'u') origin, "
        "CAST(NULL AS TIMESTAMP) timestamp, 'x' event_type, '{}' details WHERE false",
    }
    for name, sql in empty.items():
        _base(spark, name, sql)
    return spark


def _rows(spark, view: str, order: str = "") -> list:
    _build(spark)
    return spark.sql(f"SELECT * FROM app__{view} {order}").collect()


def test_index_keeps_price_only_and_index_only_days(world):
    """A WMT close with no index day, and an index day with no close, both appear."""
    _base(
        world,
        "signals__temp_index_daily",
        "SELECT * FROM VALUES (DATE'2007-12-27', 3.0D, 50L), (DATE'2013-06-27', 20.0D, 50L) "
        "AS t(date, index_c, n_metros)",
    )
    _base(
        world,
        "signals__normals",
        "SELECT * FROM VALUES (361, 1.0D), (178, 18.0D), (1, 0.5D) AS t(day_of_year, normal_c)",
    )
    _base(
        world,
        "daily__prices_daily",
        "SELECT *, CAST(NULL AS TIMESTAMP) known_at FROM VALUES ('WMT', DATE'2013-06-27', 60.0D, 0.01D), "
        "('WMT', DATE'2026-01-01', 200.0D, 0.0D), ('SPY', DATE'2026-01-01', 600.0D, 0.0D) "
        "AS t(ticker, trade_date, close, ret)",
    )
    got = {r["date"]: r for r in _rows(world, "v_ui_index")}
    assert set(got) == {date(2007, 12, 27), date(2013, 6, 27), date(2026, 1, 1)}
    assert got[date(2007, 12, 27)]["close"] is None and got[date(2007, 12, 27)]["anomaly_c"] == 2.0
    assert got[date(2013, 6, 27)]["close"] == 60.0 and got[date(2013, 6, 27)]["anomaly_c"] == 2.0
    price_only = got[date(2026, 1, 1)]
    assert price_only["close"] == 200.0 and price_only["index_c"] is None
    assert price_only["normal_c"] == 0.5 and price_only["anomaly_c"] is None


def test_index_anomaly_is_null_where_the_normal_is_null(world):
    """Day 366 has too few leap years for a normal; the view leaves the anomaly null, not zero."""
    _base(
        world,
        "signals__temp_index_daily",
        "SELECT DATE'2028-12-31' date, 4.0D index_c, 50L n_metros",
    )
    _base(
        world,
        "signals__normals",
        "SELECT * FROM VALUES (365, 1.0D), (366, CAST(NULL AS DOUBLE)) AS t(day_of_year, normal_c)",
    )
    (row,) = _rows(world, "v_ui_index")
    assert row["index_c"] == 4.0 and row["normal_c"] is None and row["anomaly_c"] is None


def _events(world, state: str, expectations: str) -> None:
    _base(
        world,
        "ops__pipeline_events",
        "SELECT * FROM VALUES "
        "(named_struct('update_id', 'u0'), TIMESTAMP'2026-11-17 10:00:00', 'update_progress', "
        '\'{"update_progress":{"state":"COMPLETED"}}\'), '
        f"(named_struct('update_id', 'u1'), TIMESTAMP'2026-11-17 11:00:00', 'update_progress', "
        f'\'{{"update_progress":{{"state":"{state}"}}}}\'), '
        f"(named_struct('update_id', 'u1'), TIMESTAMP'2026-11-17 10:59:00', 'flow_progress', '{expectations}') "
        "AS t(origin, timestamp, event_type, details)",
    )


EXPECT_TWO_OVER_100 = (
    '{"flow_progress":{"data_quality":{"expectations":['
    '{"name":"close_positive","dataset":"workspace.dev_daily.prices_daily","passed_records":100,"failed_records":0},'
    '{"name":"date_known","dataset":"workspace.dev_daily.prices_daily","passed_records":98,"failed_records":2}]}}}'
)


def test_sources_counts_rows_once_per_dataset_and_sums_violations(world):
    _events(world, "COMPLETED", EXPECT_TWO_OVER_100)
    rows = [r for r in _rows(world, "v_ui_sources") if r["kind"] == "expectations"]
    assert [(r["name"], r["n"], r["n_bad"], r["state"]) for r in rows] == [
        ("prices_daily", 100, 2, "COMPLETED")
    ]


def test_sources_carries_a_failed_update_state(world):
    _events(world, "FAILED", EXPECT_TWO_OVER_100)
    rows = _rows(world, "v_ui_sources")
    assert {r["state"] for r in rows if r["kind"] in ("update", "expectations")} == {"FAILED"}


FILL_ROWS = (
    "SELECT path, cbsa_code, lead_days, TIMESTAMP'2026-11-19 01:00:00' valid_time, "
    "current_timestamp() - make_interval(0, 0, 0, age) fetched_at FROM VALUES "
    "('a', '26420', 5, 1), ('a', '26420', 5, 1), ('b', '41860', 4, 2), ('b', '41860', 5, 2), "
    "('b', '28140', 4, 2), ('old', '1', 1, 30), ('old', '2', 2, 30), ('old', '3', 3, 30), "
    "('old', '3', 4, 30) AS t(path, cbsa_code, lead_days, age)"
)


def test_sources_gives_the_worst_recent_file_shape_of_the_fill_quarantine(world):
    """Metros and metro-lead cells in the worst file fetched in the alert
    window; the 30-day-old file with 4 cells is outside it."""
    _base(world, "daily__forecast_fill_quarantine", FILL_ROWS)
    [row] = [r for r in _rows(world, "v_ui_sources") if r["kind"] == "fill_shape"]
    assert (row["name"], row["n"], row["n_bad"]) == ("forecast_fill_quarantine", 3, 2)


def test_sources_says_no_recent_file_with_a_zero_shape(world):
    _base(world, "daily__forecast_fill_quarantine", FILL_ROWS + " WHERE age = 30")
    [row] = [r for r in _rows(world, "v_ui_sources") if r["kind"] == "fill_shape"]
    assert (row["n"], row["n_bad"]) == (0, 0)


def test_fn_ui_sources_leaves_out_the_fill_shape(world):
    """A sum of n over the function counted metro-lead cells as rows checked."""
    _base(world, "daily__forecast_fill_quarantine", FILL_ROWS)
    _build(world)
    [fn] = [f for f in views.functions(N) if ".fn_ui_sources(" in f]
    rows = world.sql(_local(fn.split(" RETURN ", 1)[1])).collect()
    assert rows and all(r["kind"] != "fill_shape" for r in rows)


def test_sources_counts_a_null_sha_as_bad(world):
    _base(
        world,
        "raw__raw_envelopes",
        "SELECT * FROM VALUES ('alphavantage', TIMESTAMP'2026-11-17 10:05:00', CAST(NULL AS BOOLEAN), 200), "
        "('alphavantage', TIMESTAMP'2026-11-17 11:05:00', true, 200) AS t(source, fetched_at, sha_ok, http_status)",
    )
    (row,) = [r for r in _rows(world, "v_ui_sources") if r["kind"] == "source"]
    assert (row["name"], row["n_bad"]) == ("alphavantage", 1)


def _signal(world, rows: str) -> None:
    _base(
        world,
        "signals__signal",
        "SELECT as_of, target_date, h AS horizon_days, action, 0.1D score, 0.6D confidence, "
        "array('r') reasons, 'm' model_name, '1' model_version, "
        '\'{"forecast_anomaly_c": 1.5, "last_close": 200.0}\' features, '
        "CAST(NULL AS TIMESTAMP) inputs_max_known_at, 'WMT' ticker, CAST(NULL AS DOUBLE) est_price, 'hold' est_reason, CAST(NULL AS STRING) est_basis "
        f"FROM VALUES {rows} AS t(as_of, target_date, h, action)",
    )


def test_calls_signal_only_is_no_call_and_duplicates_are_named(world):
    a0, a1 = "TIMESTAMP'2026-11-17 09:30:00'", "TIMESTAMP'2026-11-17 10:30:00'"
    _signal(
        world,
        f"({a0}, DATE'2026-11-18', 1, 'buy'), ({a1}, DATE'2026-11-18', 1, 'buy'), "
        f"({a1}, DATE'2026-11-19', 2, 'sell')",
    )
    _base(
        world,
        "signals__recommendations",
        f"SELECT {a1} as_of, DATE'2026-11-18' target_date, 1 horizon_days, 'buy' signal_action, "
        "'hold' action, 'why' rationale, 'low confidence' downgrade_reason, 'tr-1' trace_id, "
        "'sonnet' llm_model_id, '1' agent_version, TIMESTAMP'2026-11-17 10:41:00' written_at",
    )
    _base(
        world,
        "signals__v_recommendation_audit",
        f"SELECT {a1} as_of, DATE'2026-11-18' target_date, CAST(NULL AS STRING) problem, 2L copies",
    )
    got = {(r["as_of"], r["target_date"]): r for r in _rows(world, "v_ui_calls")}
    called = got[(datetime(2026, 11, 17, 10, 30), date(2026, 11, 18))]
    assert (called["shown_action"], called["signal_action"], called["source"]) == (
        "hold",
        "buy",
        "agent",
    )
    assert called["audit_problem"] == "duplicate" and called["forecast_anomaly_c"] == 1.5
    signal_only = got[(datetime(2026, 11, 17, 10, 30), date(2026, 11, 19))]
    assert signal_only["shown_action"] is None and signal_only["source"] == "signal_only"
    assert signal_only["audit_problem"] is None
    outlook = _rows(world, "v_ui_outlook")
    assert {r["as_of"] for r in outlook} == {datetime(2026, 11, 17, 10, 30)} and len(outlook) == 2


def test_status_carries_the_guard_inputs_and_read_at(world):
    (row,) = _rows(world, "v_ui_status")
    assert (row["open_incident"], row["post_verified_as_of"], row["guarded_tables"]) == (
        False,
        None,
        2,
    )
    assert row["read_at"] is not None


def test_backtest_view_carries_the_input_kind_and_the_sessions_close(spark):
    _base(
        spark,
        "signals__signal_backtest",
        "SELECT * FROM VALUES "
        "('h0:m:1', TIMESTAMP'2015-03-10 21:30:00', DATE'2015-03-10', 'sell', -3D, 1D, array(), "
        '\'m\', \'1\', \'{"forecast_anomaly_c": -3.0, "observed_date": "2015-03-02", '
        '"model_coef": -1.0, "model_threshold_c": 0.75}\', '
        "TIMESTAMP'2015-03-10 00:00:00', 'WMT', 'observed', TIMESTAMP'2026-11-18 00:00:00', "
        "-1, 0.02D, -0.021D, -0.021D, 0.0D, false, 1, 1) "
        "AS t(backtest_run_id, as_of, target_date, action, score, confidence, reasons, model_name, "
        "model_version, features, inputs_max_known_at, ticker, input_kind, created_at, "
        "pos, ret, pnl, gate_total, gate_p95, gate_pass, gate_n, gate_trades)",
    )
    _base(
        spark,
        "daily__prices_daily",
        "SELECT * FROM VALUES ('WMT', DATE'2015-03-10', 40D), ('SPY', DATE'2015-03-10', 200D) "
        "AS t(ticker, trade_date, close)",
    )
    r = spark.sql(_local(views.backtest(N))).collect()
    assert [(x.input_kind, x.anomaly_c, x.observed_date, x.close) for x in r] == [
        ("observed", -3.0, date(2015, 3, 2), 40.0)
    ]
    assert [(x.model_coef, x.model_threshold_c) for x in r] == [(-1.0, 0.75)]
    assert [(x.pos, x.pnl, x.gate_pass) for x in r] == [(-1, -0.021, False)]


def test_status_reads_the_gate_of_the_newest_backtest_run(world):
    _base(
        world,
        "signals__signal_backtest",
        "SELECT * FROM VALUES "
        "('h0:old:1', TIMESTAMP'2026-11-18 00:00:00', 0.5D, 0.1D, true, 9, 3), "
        "('h0:new:2', TIMESTAMP'2026-11-20 00:00:00', -0.2D, 0.3D, false, 8, 4) "
        "AS t(backtest_run_id, created_at, gate_total, gate_p95, gate_pass, gate_n, gate_trades)",
    )
    (row,) = _rows(world, "v_ui_status")
    got = [row[k] for k in ("gate_run_id", "gate_pass", "gate_total", "gate_p95", "gate_n")]
    assert got == ["h0:new:2", False, -0.2, 0.3, 8] and row["gate_trades"] == 4


def test_status_without_a_backtest_run_has_a_null_gate(world):
    (row,) = _rows(world, "v_ui_status")
    assert (row["gate_run_id"], row["gate_pass"]) == (None, None)


def test_calls_and_outlook_carry_the_estimate_columns(world):
    _signal(world, "(TIMESTAMP'2026-11-17 10:30:00', DATE'2026-11-18', 1, 'buy')")
    for view in ("v_ui_calls", "v_ui_outlook"):
        (row,) = _rows(world, view)
        assert {"est_price", "est_reason", "est_basis"} <= set(row.asDict())
