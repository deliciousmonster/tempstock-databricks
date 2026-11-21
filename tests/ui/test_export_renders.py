"""The export from `file://` in headless Chrome: each card's badge is the Python rule's label at WS_NOW."""

from __future__ import annotations

import html
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.ui.export_fixture import AS_OF, document
from tests.unit.ui.test_integrity import integrity
from weather_signal.serving import export

# Virtual time, not wall time; 3000 flaked under a loaded box (2026-11-19), so the page gets 10 s.
BUDGET_MS = 10000
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="the browser layer runs on the Mac (plan: macos-local)"
)


# After the page settles, copy what each uPlot chart was given into the DOM for --dump-dom.
PROBE = (
    "<script>setTimeout(function () { var p = document.createElement('pre'); p.id = 'probe';"
    " p.textContent = JSON.stringify((window.WS_CHARTS || []).map(function (u) { return {"
    " axes: u.axes.map(function (a) { return { label: a.label, scale: a.scale || 'x', side: a.side,"
    " stroke: typeof a.stroke === 'function' ? a.stroke(u) : a.stroke, ticks: a._values }; }),"
    " text: getComputedStyle(document.body).color,"
    " series: u.series.map(function (s, i) { return { label: s.label, scale: s.scale,"
    " stroke: typeof s.stroke === 'function' ? s.stroke(u, i) : s.stroke,"
    " kept_gaps: s.gaps && i > 0 ? (function () { var x0 = u.data[0][0], p = function (d) {"
    " return u.valToPos(x0 + d * 86400, 'x', true); }; return s.gaps(u, i, 0, 0, [[p(0), p(3)], [p(10), p(20)]]).length; })() : null,"
    " dots: !!(s.points && (typeof s.points.show === 'function' ? s.points.show(u, i) : s.points.show)),"
    " n: u.data[i].filter(function (v) { return v !== null; }).length, size: s.points ? s.points.size : null,"
    " hover: (function () { var k = u.data[i].findIndex(function (v) { return v !== null; });"
    " return k < 0 || i === 0 ? null : s.value(u, u.data[i][k], i, k); })() }; }),"
    " x: [u.scales.x.min, u.scales.x.max] }; }));"
    " document.body.appendChild(p); }, 1500);</script>"
)


def render(out: Path, now: str, route: str = "#/outlook") -> str:
    assert CHROME.is_file(), "Google Chrome is needed for the browser layer"
    page = (out / "index.html").read_text(encoding="utf-8")
    seam = f'<script>window.WS_NOW = "{now}";</script>\n<script src="static/app.js">'
    page = page.replace("</body>", PROBE + "</body>")
    (out / "seam.html").write_text(
        page.replace('<script src="static/app.js">', seam), encoding="utf-8"
    )
    args = [
        str(CHROME),
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        f"--virtual-time-budget={BUDGET_MS}",
        f"--user-data-dir={out / '.chrome'}",
        "--dump-dom",
        (out / "seam.html").as_uri() + route,
    ]
    return subprocess.run(args, capture_output=True, text=True, timeout=60, check=True).stdout


def badges(dom: str) -> list[tuple[str, str]]:
    found = re.findall(
        r'<span class="badge ([a-z_]+)">([^<]*)</span><span class="at-read">([^<]*)</span>', dom
    )
    return [(cls, html.unescape(text)) for cls, text, _ in found]


@pytest.mark.parametrize("now", ["2026-11-18T12:20:00Z", "2026-11-18T14:00:00Z"])
def test_cards_show_the_rule_at_the_viewer_clock(tmp_path, now):
    doc = document()
    export.write(doc, tmp_path)
    dom = render(tmp_path, now)
    status = doc["payload"]["status"]
    want = []
    for c in sorted(doc["payload"]["outlook"], key=lambda c: c["horizon_days"]):
        key = integrity.state({**c, **status}, now)
        want.append((key, integrity.label(key, status["read_at"])))
    assert badges(dom) == want and len(want) == 7
    assert {k for k, _ in want} == ({"verified"} if now.endswith("12:20:00Z") else {"stale"})
    assert "at snapshot, 12:15 UTC: verified" in html.unescape(dom)
    assert "Static snapshot built 2026-11-18T12:15:00Z" in dom
    assert "<img" not in dom.split("<main", 1)[1]


def test_without_the_seam_the_real_clock_is_used(tmp_path):
    export.write(document(), tmp_path)
    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "WS_NOW" not in page


def probe(dom: str) -> list[dict]:
    import json

    m = re.search(r'<pre id="probe">([^<]*)</pre>', dom)
    assert m, "the probe did not run"
    return json.loads(html.unescape(m.group(1)))


def check_two_axes(chart: dict, temp_axis: str) -> dict[str, dict]:
    """Owner's spec: date on x, temperature on the left y, price on the right, each axis in its series' colour."""
    x, left, right = chart["axes"]
    assert x["label"] == "Date (UTC)" and all(
        re.fullmatch(r"\d{4}-\d{2}-\d{2}", t) for t in x["ticks"]
    )
    assert x["stroke"] == chart["text"], (
        "date axis drawn in the page's text colour, not uPlot's black"
    )
    assert (left["label"], left["scale"], left["side"]) == (temp_axis, "temp", 3)
    assert (right["label"], right["scale"], right["side"]) == ("WMT close (USD)", "usd", 1)
    assert all(re.fullmatch(r"[+-]?\d+\.\d \u00b0C", t) for t in left["ticks"]), left["ticks"]
    assert all(re.fullmatch(r"\$\d+(\.\d\d)?", t) for t in right["ticks"] or []), right["ticks"]
    series = {s["label"]: s for s in chart["series"][1:]}
    temp = [s for s in series.values() if s["scale"] == "temp"]
    assert len(temp) == 1 and temp[0]["stroke"] == left["stroke"]
    lines = [
        s
        for s in series.values()
        if s["scale"] == "usd" and "(" not in s["label"] or "carried" in s["label"]
    ]
    assert len(lines) == 1 and lines[0]["stroke"] == right["stroke"] != left["stroke"]
    dotted = sorted(k for k, s in series.items() if s["dots"])
    assert dotted == sorted(k for k in series if "(" in k and "carried" not in k), (
        "only triggers are dots"
    )
    return series


def test_the_history_chart_has_both_axes_and_past_trigger_dots(tmp_path):
    """Owner, 2026-11-18: price and anomaly on one chart, trigger points as dots."""
    export.write(document(), tmp_path)
    dom = render(tmp_path, "2026-11-18T12:20:00Z", "#/history")
    charts = probe(dom)
    assert len(charts) == 1
    s = check_two_axes(charts[0], "Temperature anomaly (\u00b0C)")
    assert s["WMT close"]["n"] == 48 and s["Temperature index anomaly"]["n"] == 48
    dots = {k: v["n"] for k, v in s.items() if "(" in k}
    # PAST: one agent buy, one model sell, one model buy; the two holds are not drawn.
    assert dots == {
        "buy (agent call)": 1,
        "sell (agent call)": 0,
        "buy (model signal)": 1,
        "sell (model signal)": 1,
    }
    assert 'class="u-legend' in dom


def test_the_outlook_chart_has_both_axes_and_recommended_dots(tmp_path):
    """Owner, 2026-11-18: the 7-day forecast with recommended trigger points as dots."""
    export.write(document(), tmp_path)
    dom = html.unescape(render(tmp_path, "2026-11-18T12:20:00Z"))
    charts = probe(dom)
    assert len(charts) == 1
    s = check_two_axes(charts[0], "Forecast anomaly (\u00b0C)")
    assert s["Forecast anomaly"]["n"] == 2
    assert (
        s["Last close, carried forward (not a forecast)"]["n"] == 0
    )  # the fixture has no last close
    assert "the model gives no price path" in dom


def test_the_outlook_dots_sit_on_the_carried_close(tmp_path):
    export.write(document(last_close=210.0), tmp_path)
    s = check_two_axes(
        probe(render(tmp_path, "2026-11-18T12:20:00Z"))[0], "Forecast anomaly (\u00b0C)"
    )
    assert s["Last close, carried forward (not a forecast)"]["n"] == 7
    assert (s["buy (agent call)"]["n"], s["sell (agent call)"]["n"]) == (1, 1)
    assert (s["buy (model signal)"]["n"], s["sell (model signal)"]["n"]) == (0, 0)


def test_each_hold_shows_its_reason_in_plain_words(tmp_path):
    """The App never showed signal.reasons (open UI item)."""
    export.write(document(), tmp_path)
    text = html.unescape(render(tmp_path, "2026-11-18T12:20:00Z"))
    assert "hold reason: no climate normal for this date" in text
    assert "hold reason: forecast quarantined by the guard; prices not available in time" in text
    assert "hold reason: the model's own call, inputs complete" in text
    assert text.count("hold reason:") == 3
    call = html.unescape(
        render(tmp_path, "2026-11-18T12:20:00Z", "#/call/2026-11-18T10:30:00Z/2026-11-22")
    )
    assert "<dt>hold reason</dt><dd>no climate normal for this date</dd>" in call


def test_a_missing_anomaly_names_its_cause_and_says_no_normal_only_for_no_normal(tmp_path):
    """2026-11-19 dev: every stale_prices hold read "no normal" while the normal existed."""
    export.write(document(), tmp_path)
    text = html.unescape(render(tmp_path, "2026-11-18T12:20:00Z"))
    lines = re.findall(r'<div class="signal">(model signal: [^<]*)</div>', text)
    assert lines == [
        "model signal: buy, anomaly 1.50 C",
        "model signal: buy, anomaly -1.50 C",
        "model signal: buy, anomaly not recorded",
        "model signal: buy, anomaly not recorded",
        "model signal: hold, no normal",
        "model signal: hold, forecast quarantined",
        "model signal: hold, anomaly not recorded",
    ]
    call = html.unescape(
        render(tmp_path, "2026-11-18T12:20:00Z", "#/call/2026-11-18T10:30:00Z/2026-11-21")
    )
    assert "<dt>forecast anomaly (C)</dt><dd>anomaly not recorded</dd>" in call


def test_quarantined_fill_runs_are_not_a_red_violation_and_a_real_one_still_is(tmp_path):
    """2026-11-19 dev: forecast_fill_check's single-metro runs were drawn as 3 red violations."""
    exp = {"kind": "expectations", "layer": "daily", "at": AS_OF, "state": "COMPLETED"}
    rows = [
        exp | {"name": "forecast_fill_check", "n": 14700, "n_bad": 3},
        exp | {"name": "forecast_fill_summary", "n": 3, "n_bad": 0},
        exp | {"name": "prices_daily", "n": 24566, "n_bad": 1},
        {
            "kind": "fill_shape",
            "layer": "daily",
            "name": "forecast_fill_quarantine",
            "at": None,
            "state": None,
            "n": 1,
            "n_bad": 1,
        },
    ]
    export.write(document(source_rows=rows), tmp_path)
    chips = dict(
        (html.unescape(t).split(":")[0], (cls, html.unescape(t)))
        for cls, t in re.findall(
            r'<span class="chip ([a-z]*)">([^<]*)</span>', render(tmp_path, "2026-11-18T12:20:00Z")
        )
    )
    cls, text = chips["forecast_fill_check"]
    assert cls == "" and "3 runs quarantined" in text and "violations" not in text
    assert chips["prices_daily"][0] == "bad" and "1 violations" in chips["prices_daily"][1]
