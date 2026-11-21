"""Source states: cadence keys are the foundation's own strings, and nothing is hidden."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from weather_signal.serving import sources
from weather_signal.signal import checks

NOW = datetime(2026, 11, 18, 12, 0)
INGEST = Path(__file__).resolve().parents[3] / "src" / "weather_signal" / "ingest"


def test_cadence_keys_are_the_foundations_source_strings():
    text = "".join(p.read_text(encoding="utf-8") for p in INGEST.glob("*.py"))
    for key in sources.CADENCE:
        assert re.search(rf"['\"]{re.escape(key)}['\"]", text), key


def test_states():
    assert sources.source_state("alphavantage", NOW - timedelta(minutes=90), NOW) == "ok"
    assert sources.source_state("alphavantage", NOW - timedelta(hours=3), NOW) == "stale"
    assert sources.source_state("open-meteo-era5", NOW - timedelta(hours=30), NOW) == "ok"
    assert sources.source_state("yahoo", NOW - timedelta(days=90), NOW) == "backfill"
    assert sources.source_state("alphavantage", None, NOW) == "not landed"


def test_strip_names_unlabelled_and_missing_sources():
    rows = [
        {"kind": "source", "name": "somewhere-new", "at": NOW},
        {"kind": "source", "name": "yahoo", "at": datetime(2026, 11, 17)},
        {"kind": "fresh", "name": "temp_index_daily", "at": datetime(2007, 12, 27)},
    ]
    got = {(r["kind"], r["name"]): r for r in sources.strip(rows, NOW)}
    assert got[("source", "somewhere-new")]["state"] == "unlabelled source"
    assert got[("source", "yahoo")]["label"] == "backfill, 2026-11-17"
    assert got[("source", "alphavantage")]["state"] == "not landed"
    assert got[("fresh", "temp_index_daily")]["label"] == "latest data day"


def test_an_update_not_completed_makes_no_clean_claim():
    rows = [
        {"kind": "expectations", "name": "prices_daily", "state": "FAILED"},
        {"kind": "update", "name": "last_update", "state": "COMPLETED"},
    ]
    labels = [r["label"] for r in sources.strip(rows, NOW)[:2]]
    assert labels == ["pipeline update FAILED", "checked"]


def _fill(n_bad: int, cells: int | None, metros: int | None) -> list[dict]:
    rows = [
        {
            "kind": "expectations",
            "name": "forecast_fill_check",
            "state": "COMPLETED",
            "n_bad": n_bad,
        },
        {"kind": "expectations", "name": "forecast_fill_summary", "state": "COMPLETED", "n_bad": 0},
        {"kind": "expectations", "name": "prices_daily", "state": "COMPLETED", "n_bad": 2},
    ]
    if cells is not None:
        rows.append(
            {"kind": "fill_shape", "name": "forecast_fill_quarantine", "n": cells, "n_bad": metros}
        )
    return rows


def test_the_fill_check_counts_quarantined_runs_and_names_the_check_that_flags_a_fill():
    """2026-11-19 dev: 3 of 14,700 single-metro runs read as violations in red."""
    got = {r["name"]: r for r in sources.strip(_fill(3, 1, 1), NOW)}
    assert got["forecast_fill_check"]["counted"] is True
    assert "one metro at one lead in each file" in got["forecast_fill_check"]["label"]
    assert not got["forecast_fill_summary"]["counted"] and not got["prices_daily"]["counted"]
    assert "forecast_fill_quarantine" not in got, "the shape row is read, not shown"


@pytest.mark.parametrize(
    ("cells", "metros", "said"),
    [(6, 1, "1 metro over 6 metro-lead cells"), (3, 3, "3 metros over 3 metro-lead cells")],
)
def test_a_file_with_more_than_one_quarantined_cell_stays_red_and_says_what_it_is(
    cells, metros, said
):
    """One metro over leads 1 to 6, or 2 to 4 metros at a lead, passes the summary."""
    [r] = [
        r for r in sources.strip(_fill(6, cells, metros), NOW) if r["name"] == "forecast_fill_check"
    ]
    assert (
        not r["counted"] and r["label"].startswith("worst file in the last") and said in r["label"]
    )


def test_without_the_quarantine_shape_nothing_is_called_weather():
    [r] = [
        r for r in sources.strip(_fill(3, None, None), NOW) if r["name"] == "forecast_fill_check"
    ]
    assert not r["counted"] and r["label"] == "checked"


def test_the_label_names_the_fill_bar_the_checks_use(monkeypatch):
    """The bar is checks.FILL_ALERT_METROS, not a literal."""
    monkeypatch.setattr(checks, "FILL_ALERT_METROS", 7)
    [r] = [r for r in sources.strip(_fill(3, 1, 1), NOW) if r["name"] == "forecast_fill_check"]
    assert "7-metro fill bar" in r["label"] and "5-metro" not in r["label"]


def test_a_fill_check_in_a_failed_update_is_not_called_weather():
    rows = [{"kind": "expectations", "name": "forecast_fill_check", "state": "FAILED", "n_bad": 3}]
    [r] = sources.strip(rows, NOW)[:1]
    assert not r["counted"] and r["label"] == "pipeline update FAILED"


@pytest.mark.parametrize(
    "summary",
    [
        [
            {
                "kind": "expectations",
                "name": "forecast_fill_summary",
                "state": "COMPLETED",
                "n_bad": 1,
            }
        ],
        [],
    ],
)
def test_the_fill_check_is_called_weather_only_when_the_fill_check_ran_clean(summary):
    """The label asserted single metros without looking; a fill row keeps it red."""
    rows = [
        {"kind": "expectations", "name": "forecast_fill_check", "state": "COMPLETED", "n_bad": 6},
        *summary,
    ]
    r = next(r for r in sources.strip(rows, NOW) if r["name"] == "forecast_fill_check")
    assert not r["counted"] and r["label"] == "checked"


def test_with_no_recent_quarantined_file_older_runs_rest_on_the_summary():
    """One old multi-cell file kept the check red; the shape covers the alert window."""
    [r] = [r for r in sources.strip(_fill(3, 0, 0), NOW) if r["name"] == "forecast_fill_check"]
    days = checks.FILL_ALERT_WINDOW.days
    assert r["counted"] and f"no file quarantined in the last {days} days" in r["label"]


def test_the_worst_file_label_names_the_window():
    [r] = [r for r in sources.strip(_fill(6, 6, 1), NOW) if r["name"] == "forecast_fill_check"]
    days = checks.FILL_ALERT_WINDOW.days
    assert not r["counted"] and r["label"].startswith(f"worst file in the last {days} days: ")


def test_intraday_is_display_only_never_stale_and_labelled_by_its_last_landing():
    old = NOW - timedelta(days=3)  # a weekend: no session, no fetch
    assert sources.source_state("yahoo-intraday", old, NOW) == "display only"
    rows = sources.strip([{"kind": "source", "name": "yahoo-intraday", "at": old}], NOW)
    (row,) = [r for r in rows if r["name"] == "yahoo-intraday"]
    assert row["label"] == "display only, 2026-11-15" and row["state"] != "stale"
