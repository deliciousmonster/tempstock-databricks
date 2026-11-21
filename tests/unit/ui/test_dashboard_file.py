"""The dashboard file reads only `v_ui_*`, unqualified, so the bundle's catalog and schema apply."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

FILE = Path(__file__).resolve().parents[3] / "aibi" / "weather_signal.lvdash.json"
DASH = json.loads(FILE.read_text(encoding="utf-8"))
DATASETS = {d["name"]: " ".join(d["queryLines"]) for d in DASH["datasets"]}
WIDGETS = [w["widget"] for p in DASH["pages"] for w in p["layout"]]
NOTICE = "Not investment advice"


def _tables(sql: str) -> list[str]:
    """FROM and JOIN targets, less the query's own CTE names and the aliases m and a."""
    targets = re.findall(r"\b(?:FROM|JOIN)\s+([\w.`]+)", sql, re.I)
    ctes = set(re.findall(r"(?:\bWITH|,)\s+(\w+)\s+AS\s*\(", sql, re.I))
    return [t for t in targets if t not in ("m", "a") and t not in ctes]


@pytest.mark.parametrize("name", sorted(DATASETS))
def test_dataset_reads_only_ui_views_unqualified(name):
    sql = DATASETS[name]
    tables = _tables(sql)
    assert tables and all(re.fullmatch(r"v_ui_\w+", t) for t in tables), tables
    ds = next(d for d in DASH["datasets"] if d["name"] == name)
    assert "catalog" not in ds and "schema" not in ds


def test_widgets_name_existing_datasets_and_fields():
    for w in WIDGETS:
        for q in w.get("queries", []):
            sql = DATASETS[q["query"]["datasetName"]]
            for f in q["query"]["fields"]:
                assert re.search(rf"\b{f['name']}\b", sql), (w["name"], f["name"])


def test_every_page_carries_the_notice_and_the_guard_inputs():
    for page in DASH["pages"]:
        texts = " ".join(
            "".join(w["widget"].get("multilineTextboxSpec", {}).get("lines", []))
            for w in page["layout"]
        )
        assert NOTICE in texts, page["name"]
        names = {w["widget"]["name"] for w in page["layout"]}
        assert f"{page['name']}-guard" in names


def test_the_dashboard_makes_no_verified_claim():
    assert "'verified'" not in json.dumps(DASH)


def test_the_history_page_does_not_bound_the_close_to_index_days():
    """The close series must not depend on the index having a row that day."""
    assert "index_c" not in DATASETS["close"]


def test_widget_names_are_unique():
    names = [w["name"] for w in WIDGETS]
    assert len(names) == len(set(names))


def test_every_chart_titles_its_axes_with_units():
    """Owner, 2026-11-18: axis titles on every chart; a raw field name is not a title."""
    units = {
        "forecast_anomaly_c": "\u00b0C",
        "anomaly_c": "\u00b0C",
        "close": "USD",
        "price": "USD",
    }
    charts = [
        w["widget"]
        for p in json.loads(FILE.read_text(encoding="utf-8"))["pages"]
        for w in p["layout"]
        if w["widget"].get("spec", {}).get("widgetType") in ("bar", "line", "scatter")
    ]
    assert len(charts) == 6
    for w in charts:
        for axis, e in w["spec"]["encodings"].items():
            assert e["displayName"] != e["fieldName"], (w["name"], axis)
            assert e["displayName"][0].isupper(), (w["name"], axis)
            if e["fieldName"] in units:
                assert units[e["fieldName"]] in e["displayName"], (w["name"], axis)


def test_the_intraday_page_draws_closes_calls_and_est_price_from_the_intraday_view():
    """The same chart as the App: hourly closes, a point per call, and each call's est_price."""
    chart = DATASETS["intraday_chart"]
    assert "FROM v_ui_intraday" in chart and "FROM v_ui_calls" in chart
    assert "'hourly close'" in chart and "' call'" in chart and "'est_price'" in chart
    assert "horizon_days = 0" in chart
    table = DATASETS["intraday_calls"]
    assert "concat_ws(' ', '-', est_reason)" in table
    names = {w["name"] for w in WIDGETS}
    assert {"intraday-chart", "intraday-calls", "intraday-guard"} <= names


@pytest.mark.parametrize("name", ["intraday_chart", "intraday_calls"])
def test_the_intraday_span_ends_an_hour_past_the_sessions_own_close(name):
    """An early close (18:00Z) must not keep a fixed 7 h span from the open."""
    sql = DATASETS[name]
    assert "session_close" in sql and "s.closed + INTERVAL 1 HOUR" in sql
    assert "INTERVAL 7 HOURS" not in sql


def test_a_cte_name_is_not_taken_for_a_table_but_a_real_table_still_is():
    sql = "WITH bars AS (SELECT 1 FROM v_ui_intraday), s AS (SELECT 1 FROM bars) SELECT * FROM daily.x"
    assert _tables(sql) == ["v_ui_intraday", "daily.x"]
