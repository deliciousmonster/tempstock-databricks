"""The Intraday page in headless Chrome: hourly closes, a dot per call, est_price or "-" and its reason."""

from __future__ import annotations

import html
import re

from tests.ui.export_fixture import document
from tests.ui.test_export_renders import probe, pytestmark, render  # noqa: F401
from weather_signal.serving import export

# Horizon 0 is the only intraday call; its est is a value here and null with a reason below.
WITH_EST = {0: (201.25, None)}
WITHOUT_EST = {0: (None, "no_champion")}


def _cells(dom: str) -> list[str]:
    body = dom.split("Recent calls", 1)[1]
    return [html.unescape(c) for c in re.findall(r"<td>([^<]*)</td>", body)]


def test_the_chart_draws_closes_a_call_dot_and_its_est_price(tmp_path):
    export.write(document(est=WITH_EST), tmp_path)
    dom = render(tmp_path, "2026-11-18T12:20:00Z", "#/intraday")
    (chart,) = probe(dom)
    x, y = chart["axes"]
    assert x["label"] == "Time (UTC)" and y["label"] == "WMT price (USD)"
    series = {s["label"]: s for s in chart["series"][1:]}
    assert series["WMT hourly close"]["n"] == 14 and not series["WMT hourly close"]["dots"]
    assert series["buy call"]["n"] == 1 and series["buy call"]["dots"]
    assert series["est_price"]["n"] == 1
    assert "for display only" in dom
    assert _cells(dom)[-1] == "$201.25"


def test_a_null_est_price_reads_dash_and_its_reason(tmp_path):
    export.write(document(est=WITHOUT_EST), tmp_path)
    dom = render(tmp_path, "2026-11-18T12:20:00Z", "#/intraday")
    assert _cells(dom)[-1] == "- no_champion"
    (chart,) = probe(dom)
    assert {s["label"]: s["n"] for s in chart["series"][1:]}["est_price"] == 0


def test_before_any_bars_the_page_says_so(tmp_path):
    export.write(document(), tmp_path)
    dom = render(tmp_path, "2026-11-18T12:20:00Z", "#/intraday")
    assert "No intraday bars yet" in dom and probe(dom) == []
