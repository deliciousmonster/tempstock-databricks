"""The baseline: the path rule strips the scope's own raw volume and fails any other value."""

from datetime import date

import pytest

from weather_signal.deploy import baseline, scope

S = scope.build("p", "scratch", "t1_", "workspace")
CUT = date(2026, 11, 10)


def test_the_prefix_is_the_scopes_own_raw_schema():
    assert baseline.volume_prefix(S) == "/Volumes/workspace/t1_raw/"


def test_path_is_stripped_floats_formatted_and_fetch_clocks_left_out():
    cols = [("path", "string"), ("temp_c", "double"), ("fetched_at", "timestamp"), ("date", "date")]
    q = baseline.digest_sql(S, "daily", "weather_observed_daily", cols, CUT)
    assert "substr(`path`, 27)" in q
    assert "format_string('%.12g', `temp_c`)" in q
    assert "fetched_at" not in q
    assert "WHERE `date` <= TIMESTAMP'2026-11-10 23:59:59'" in q


def test_the_outside_check_compares_the_whole_prefix():
    q = baseline.outside_sql(S, "daily", "prices_daily")
    assert "path IS NULL" in q and "substr(path, 1, 26) <> '/Volumes/workspace/t1_raw/'" in q


def fake_sql(outside):
    def sql(q):
        if q.startswith("DESCRIBE"):
            return [
                {"col_name": "path", "data_type": "string"},
                {"col_name": "v", "data_type": "int"},
                {"col_name": "# Partitioning", "data_type": ""},
            ]
        if "min(path)" in q:
            return [
                {
                    "n": outside,
                    "example": "/Volumes/workspace/dev_raw/raw/x" if outside else None,
                }
            ]
        return [{"n": 10, "digest": 12345}]

    return sql


def test_a_path_outside_the_volume_fails_the_compare_naming_the_table():
    base = baseline.write(fake_sql(0), S, CUT, {"params": "{}", "fit_as_of": "t", "fit": "f"})
    out = baseline.compare(base, lambda c: baseline.write(fake_sql(2), S, c, None))
    assert len(out) == 1 and "daily.metros.path: 2 rows outside" in out[0]


def test_equal_scopes_compare_empty_and_a_changed_champion_is_named():
    tags = {"params": "{}", "fit_as_of": "t", "fit": "f"}
    base = baseline.write(fake_sql(0), S, CUT, tags)
    assert baseline.compare(base, lambda c: baseline.write(fake_sql(0), S, c, tags)) == []
    out = baseline.compare(base, lambda c: baseline.write(fake_sql(0), S, c, {**tags, "fit": "g"}))
    assert out and out[0].startswith("champion champion:")


def test_a_gate_that_flips_on_newer_sessions_does_not_fail_the_compare():
    tags = {"params": "{}", "fit_as_of": "t", "fit": "f", "period_through": "2026-12-31"}
    base = baseline.write(fake_sql(0), S, CUT, {**tags, "gate_pass": "true"})
    now = {**tags, "gate_pass": "false", "gate_total": "-0.1"}
    assert baseline.compare(base, lambda c: baseline.write(fake_sql(0), S, c, now)) == []


def test_another_format_is_refused_before_any_read():
    out = baseline.compare({"format": 0}, lambda c: pytest.fail("read"))
    assert out == ["baseline format 0 is not 1"]
