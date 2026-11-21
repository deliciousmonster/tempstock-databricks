"""The fill checks count; they never halt or drop (the dev update failed on one, 2026-11-18)."""

from tests.spark.pipeline_graph import load


def test_the_fill_expectations_are_counted_and_none_halts_the_pipeline(spark):
    tables = load()
    fill = {
        n: kind
        for t in ("forecast_fill_check", "forecast_fill_summary")
        for kind, n, _ in tables[t]
    }
    assert fill == {"lead_not_filled": "expect", "fill_one_metro": "expect"}
    for t in ("forecast_fill_quarantine", "forecast_quarantined_days", "forecast_daily"):
        assert tables[t] == []


HALTING = {"expect_or_fail", "expect_all_or_fail"}
# The checks that may stop an update: reference data and the price guards.
MAY_HALT = {"fifty", "weights", "continental", "split_guard", "known_at_bound", "in_calendar"}


def test_only_the_reference_and_price_guards_may_halt_an_update(spark):
    halting = {n for exps in load().values() for kind, n, _ in exps if kind in HALTING}
    assert halting == MAY_HALT


def test_the_recorder_reads_streaming_table_expectations(spark):
    assert load()["forecast_hourly"] == [
        ("expect_all_or_drop", "temp_bounds", "temp_c BETWEEN -60 AND 60")
    ]


def test_the_recorder_sees_the_halting_checks_the_pipeline_does_keep(spark):
    kinds = {n: kind for exps in load().values() for kind, n, _ in exps}
    assert kinds["split_guard"] == "expect_or_fail" and kinds["fifty"] == "expect_all_or_fail"


def test_the_price_guards_the_pipeline_declares_are_the_ones_the_tests_evaluate(spark):
    from weather_signal.ingest.validate import SPLIT_GUARD
    from weather_signal.pipeline import transforms

    conds = {n: c for kind, n, c in load()["prices_daily"]}
    assert conds["split_guard"] == transforms.SPLIT_OK
    assert conds["known_at_bound"] == transforms.PRICE_KNOWN_AT_OK
    assert f"< {SPLIT_GUARD}" in transforms.SPLIT_OK
