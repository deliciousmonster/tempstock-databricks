"""Every statement the App sends: constant SELECTs over `v_ui_*`, request values as named parameters."""

SQL = {
    "call": (
        "SELECT * FROM v_ui_calls WHERE as_of = CAST(:as_of AS TIMESTAMP) "
        "AND target_date = CAST(:target_date AS DATE)"
    ),
    "evolution": (
        "SELECT * FROM v_ui_calls WHERE target_date = CAST(:target_date AS DATE) "
        "QUALIFY row_number() OVER (PARTITION BY horizon_days ORDER BY as_of DESC) = 1 "
        "ORDER BY horizon_days DESC"
    ),
    "calls": (
        "SELECT as_of, target_date, horizon_days, signal_action, signal_confidence, "
        "forecast_anomaly_c, shown_action, source, audit_problem, last_close, written_at FROM v_ui_calls "
        "WHERE horizon_days = CAST(:horizon AS INT) "
        "AND target_date BETWEEN CAST(:start AS DATE) AND CAST(:end AS DATE) "
        "QUALIFY row_number() OVER (PARTITION BY target_date ORDER BY as_of DESC) = 1"
    ),
    # Owner, 2026-11-18: the History chart runs from the backtest's first session; one run, newest.
    "backtest": (
        "SELECT target_date, action, anomaly_c, input_kind, model_coef, model_threshold_c, "
        "ticker, close, backtest_run_id FROM v_ui_backtest WHERE backtest_run_id = "
        "(SELECT backtest_run_id FROM v_ui_backtest ORDER BY created_at DESC LIMIT 1) "
        "QUALIFY row_number() OVER (PARTITION BY target_date ORDER BY created_at DESC) = 1 "
        "ORDER BY target_date"
    ),
    "index_full": (
        "SELECT date, anomaly_c, ticker, close FROM v_ui_index "
        "WHERE date >= (SELECT min(target_date) FROM v_ui_backtest) ORDER BY date"
    ),
    "index": (
        "SELECT * FROM v_ui_index WHERE date BETWEEN CAST(:start AS DATE) AND CAST(:end AS DATE) "
        "ORDER BY date"
    ),
}
