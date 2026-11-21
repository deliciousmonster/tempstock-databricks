"""The `v_ui_*` views and `fn_ui_*` functions as `setup` left them on dev, read over Connect."""

from __future__ import annotations

import pytest

from weather_signal.serving import sources, views


@pytest.mark.parametrize("view", views.VIEWS)
def test_each_view_reads(spark, n, view):
    spark.sql(f"SELECT * FROM {n.schema('signals')}.{view} LIMIT 1").collect()


def test_index_carries_every_close(spark, n):
    """On dev the index and the prices do not overlap; every close still appears."""
    signals, daily = n.schema("signals"), n.schema("daily")
    got = spark.sql(
        f"SELECT count(close) AS closes, count(index_c) AS days, count(*) AS rows, "
        f"count_if(index_c IS NOT NULL AND normal_c IS NOT NULL AND anomaly_c IS NULL) AS lost "
        f"FROM {signals}.v_ui_index"
    ).first()
    want = spark.sql(
        f"SELECT count(*) AS c FROM {daily}.prices_daily WHERE ticker = 'WMT'"
    ).first()["c"]
    days = spark.sql(f"SELECT count(*) AS c FROM {signals}.temp_index_daily").first()["c"]
    assert (got["closes"], got["days"], got["lost"]) == (want, days, 0)
    assert got["rows"] >= max(want, days)


def test_every_landed_source_is_a_cadence_key(spark, n):
    rows = spark.sql(f"SELECT DISTINCT source FROM {n.schema('raw')}.raw_envelopes").collect()
    assert {r["source"] for r in rows} <= set(sources.CADENCE)


def test_status_function_returns_the_guard_inputs(spark, n):
    rows = spark.sql(f"SELECT * FROM {n.schema('signals')}.fn_ui_status()").collect()
    assert len(rows) == 1 and rows[0]["guarded_tables"] == 2 and rows[0]["read_at"] is not None


def test_call_functions_read(spark, n):
    signals = n.schema("signals")
    out = spark.sql(f"SELECT * FROM {signals}.fn_ui_outlook()").collect()
    assert {r["as_of"] for r in out} <= {
        spark.sql(f"SELECT max(as_of) m FROM {signals}.signal").first()["m"]
    }
    spark.sql(f"SELECT * FROM {signals}.fn_ui_runs(24)").collect()
    spark.sql(f"SELECT * FROM {signals}.fn_ui_sources()").collect()
    spark.sql(f"SELECT * FROM {signals}.fn_ui_calls(current_date())").collect()
