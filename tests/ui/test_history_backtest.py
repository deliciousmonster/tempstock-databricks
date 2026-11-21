"""Owner, 2026-11-18: History runs from the backtest's first session, with its dots, named for the
stock its rows carry."""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from tests.ui.test_export_renders import BUDGET_MS, CHROME, PROBE, probe

STATIC = Path(__file__).resolve().parents[2] / "app" / "static"
FIRST, LAST = date(2013, 6, 27), date(2026, 11, 17)
RUN = "h0:threshold_wf:1"
LABEL = "backtest (walk-forward threshold model)"
ACTIONS = ("buy", "hold", "sell", "buy")


def _days() -> list[date]:
    return [FIRST + timedelta(days=i) for i in range((LAST - FIRST).days + 1)]


def _backtest(coef: float = -1.0) -> list[dict]:
    sessions = [d for d in _days() if d.weekday() < 5]
    return [
        {
            "target_date": d.isoformat(),
            "action": ACTIONS[i % 4],
            "anomaly_c": 1.25,
            "input_kind": "observed" if d.year != 2021 else "forecast",
            "model_coef": coef,
            "model_threshold_c": 0.75,
            "close": 100.0,
            "ticker": "WMT",
            "backtest_run_id": RUN,
        }
        for i, d in enumerate(sessions)
    ]


MEASURED = {
    "backtest_run_id": RUN,
    "first": "1991-01-10",
    "last": "2026-11-18",
    "n_days": 9029,
    "n_trades": 6000,
    "buys": 4000,
    "sells": 2000,
    "hit_rate": 0.5123,
    "always_buy": 0.52,
    "random_mean": 0.505,
    "random_band": [0.4923, 0.5177],
    "share_random_at_or_above": 0.1301,
    "trades_by_input": {"forecast": 330, "observed": 5670},
    "by_input": {
        "forecast": {
            "n_trades": 330,
            "hit_rate": 0.5182,
            "always_buy": 0.5281,
            "random_band": [0.47, 0.57],
        },
        "observed": {
            "n_trades": 5670,
            "hit_rate": 0.5121,
            "always_buy": 0.5195,
            "random_band": [0.4921, 0.5179],
        },
    },
}


def _api(
    run: str = RUN, coef: float = -1.0, ticker: str = "WMT", measured: dict | None = MEASURED
) -> dict[str, dict]:
    index = [
        {"date": d.isoformat(), "anomaly_c": 0.5, "close": 100.0 + i / 100, "ticker": ticker}
        for i, d in enumerate(_days())
    ]
    bt = [r | {"backtest_run_id": run, "ticker": ticker} for r in _backtest(coef)]
    live = [
        {
            "target_date": "2026-11-10",
            "horizon_days": 0,
            "as_of": "2026-11-10T10:30:00Z",
            "shown_action": "buy",
            "signal_action": "buy",
            "signal_confidence": 0.6,
        }
    ]
    return {
        "index": {"data": index[-400:]},
        "calls?horizon=0": {"data": live},
        "backtest": {"data": {"backtest": bt, "index": index, "measured": measured}},
    }


def render(tmp: Path, api: dict[str, dict], click: str | None = None) -> str:
    """The App's own page with fetch answered from `api`; `click` presses a range button first."""
    assert CHROME.is_file(), "Google Chrome is needed for the browser layer"
    shutil.copytree(STATIC, tmp / "static")
    stub = (
        "<script>window.WS_NOW = '2026-11-18T12:20:00Z'; var API = "
        + json.dumps(api)
        + "; window.fetch = function (u) { var k = String(u).replace(/^api\\//, '');"
        " return Promise.resolve(k in API ? { ok: true, json: function () { return API[k]; } }"
        " : { ok: false, status: 503 }); };</script>\n"
    )
    clicker = (
        "<script>setTimeout(function () { [].slice.call(document.querySelectorAll('.range button'))"
        f".filter(function (b) {{ return b.textContent === {json.dumps(click)}; }})[0].click(); }}, 900);</script>"
        if click
        else ""
    )
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    page = page.replace('<script src="snapshot.js" onerror="void 0"></script>\n', "")
    page = page.replace('<script src="static/app.js">', stub + '<script src="static/app.js">')
    (tmp / "page.html").write_text(page.replace("</body>", clicker + PROBE + "</body>"), "utf-8")
    args = [
        str(CHROME),
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        f"--virtual-time-budget={BUDGET_MS}",
        f"--user-data-dir={tmp / '.chrome'}",
        "--dump-dom",
        (tmp / "page.html").as_uri() + "#/history",
    ]
    return subprocess.run(args, capture_output=True, text=True, timeout=90, check=True).stdout


def _series(chart: dict) -> dict[str, dict]:
    return {s["label"]: s for s in chart["series"][1:]}


def test_history_runs_from_the_first_backtest_session_with_year_ticks(tmp_path):
    [chart] = probe(render(tmp_path, _api()))
    assert chart["x"][0] <= 1372291200  # 2013-06-27T00:00:00Z
    assert chart["x"][1] >= 1763337600  # 2026-11-17T00:00:00Z
    ticks = chart["axes"][0]["ticks"]
    assert ticks and all(re.fullmatch(r"\d{4}", t) for t in ticks), ticks
    assert _series(chart)["WMT close"]["n"] == len(_days())


def test_backtest_dots_are_their_own_lighter_smaller_series(tmp_path):
    s = _series(probe(render(tmp_path, _api()))[0])
    bt = _backtest()
    want = {a: sum(r["action"] == a for r in bt) for a in ("buy", "sell")}
    assert s[f"buy, {LABEL}"]["n"] == want["buy"] and s[f"sell, {LABEL}"]["n"] == want["sell"]
    assert s[f"buy, {LABEL}"]["stroke"] != s["buy (agent call)"]["stroke"]
    assert s[f"buy, {LABEL}"]["dots"] and not s["WMT close"]["dots"]
    assert s[f"buy, {LABEL}"]["size"] < s["buy (agent call)"]["size"]
    assert s["buy (agent call)"]["n"] == 1, "live calls stay as they were"


def test_backtest_hover_names_the_input_and_that_years_fit(tmp_path):
    s = _series(probe(render(tmp_path, _api()))[0])
    hover = s[f"buy, {LABEL}"]["hover"]
    assert (
        hover
        == "buy, anomaly +1.3 °C (observed), fit for 2013: sign -1, warmer sells, threshold 0.75 °C"
    )


def test_lines_join_a_weekend_and_break_across_more_than_a_week(tmp_path):
    s = _series(probe(render(tmp_path, _api()))[0])
    assert s["WMT close"]["kept_gaps"] == 1 and s["Temperature index anomaly"]["kept_gaps"] == 1


def test_a_year_before_the_first_fit_says_so(tmp_path):
    s = _series(probe(render(tmp_path, _api(coef=0.0)))[0])
    assert (
        s[f"buy, {LABEL}"]["hover"] == "buy, anomaly +1.3 °C (observed), fit for 2013: no fit yet"
    )


def _caption(dom: str) -> str:
    return html.unescape(re.search(r'<p class="caption">([^<]*)</p>', dom).group(1))


def test_the_caption_gives_the_measured_result_of_the_run_drawn(tmp_path):
    """2026-11-19 dev: the run had 9,030 rows and the page said no result was recorded."""
    text = _caption(render(tmp_path, _api()))
    assert "No measured result" not in text
    assert text.startswith(f"Backtest {RUN}, 1991-01-10 to 2026-11-18: 6,000 buy or sell days")
    assert "51.2% against 52.0% for always buying (random band 49.2% to 51.8%)" in text


def test_inside_the_band_is_not_called_no_edge(tmp_path):
    """A rate inside the band is not evidence of no edge."""
    text = _caption(render(tmp_path, _api()))
    assert "no edge" not in text and "51.8%), not distinguishable from random." in text


def test_the_forecast_days_are_reported_apart_from_the_observed(tmp_path):
    """95% of dev's trades used the observed ERA5 anomaly, not a forecast."""
    text = _caption(render(tmp_path, _api()))
    assert (
        "5,670 of them used the latest observed ERA5 anomaly" in text
        and "330 used a forecast" in text
    )
    assert "mostly the observed anomaly, not forecast skill" in text
    assert text.endswith(
        "Forecast days alone: 51.8% against 52.8% for always buying (random band 47.0% to 57.0%), "
        "not distinguishable from random."
    )


def test_a_run_with_no_forecast_trade_says_so(tmp_path):
    m = MEASURED | {
        "trades_by_input": {"forecast": 0, "observed": 6000},
        "by_input": {"forecast": None, "observed": MEASURED["by_input"]["observed"]},
    }
    assert _caption(render(tmp_path, _api(measured=m))).endswith("No trade used a forecast.")


def test_a_forecast_slice_too_small_to_measure_keeps_its_count(tmp_path):
    """A null slice with one forecast trade read "0 used a forecast"."""
    m = MEASURED | {
        "trades_by_input": {"forecast": 1, "observed": 5999},
        "by_input": {"forecast": None, "observed": MEASURED["by_input"]["observed"]},
    }
    text = _caption(render(tmp_path, _api(measured=m)))
    assert "1 used a forecast" in text and "0 used a forecast" not in text
    assert "No trade used a forecast" not in text
    assert text.endswith("Forecast days alone: too few days to measure.")


def test_a_result_without_the_input_split_claims_no_split(tmp_path):
    """No by_input printed "0 of them ... and 0 used a forecast"."""
    m = {k: v for k, v in MEASURED.items() if k not in ("by_input", "trades_by_input")}
    text = _caption(render(tmp_path, _api(measured=m)))
    assert "0 of them" not in text and "0 used a forecast" not in text
    assert "No trade used a forecast" not in text and "mostly the observed" not in text
    assert text.endswith("Input kinds were not measured for this run.")


def test_a_hit_rate_past_the_band_is_called_above_it(tmp_path):
    text = _caption(render(tmp_path, _api(measured=MEASURED | {"hit_rate": 0.53})))
    assert (
        "53.0% against 52.0% for always buying (random band 49.2% to 51.8%), above the random band."
        in text
    )


def test_a_run_with_nothing_measured_gets_no_figures(tmp_path):
    text = _caption(render(tmp_path, _api(measured=None)))
    assert text == f"No measured result is recorded for backtest run {RUN}." and "%" not in text


def test_labels_name_the_stock_the_rows_carry(tmp_path):
    dom = render(tmp_path, _api(ticker="XLP"))
    s = _series(probe(dom)[0])
    assert "XLP close" in s and "WMT close" not in s
    assert "XLP close and temperature anomaly" in dom and "XLP closes" in dom


def test_a_result_for_another_run_gets_no_figures(tmp_path):
    text = _caption(render(tmp_path, _api("h0:threshold_wf:2")))
    assert "No measured result" in text and "%" not in text


def test_the_one_year_control_zooms_to_a_year_with_month_ticks(tmp_path):
    [chart] = probe(render(tmp_path, _api(), click="1 year"))
    lo, hi = chart["x"]
    assert 360 * 86400 <= hi - lo <= 366 * 86400
    ticks = chart["axes"][0]["ticks"]
    assert ticks and all(re.fullmatch(r"\d{4}-\d{2}", t) for t in ticks), ticks


def test_without_the_backtest_the_last_400_days_are_drawn_and_said(tmp_path):
    api = _api()
    del api["backtest"]
    dom = render(tmp_path, api)
    [chart] = probe(dom)
    assert _series(chart)["WMT close"]["n"] == 400
    assert not any(LABEL in k for k in _series(chart))
    assert "this page shows the last 400 days without it" in dom


def test_an_empty_input_split_is_not_measured_not_an_error(tmp_path):
    """`trades_by_input: {}` threw in inputs() and left no caption."""
    m = MEASURED | {"trades_by_input": {}}
    assert _caption(render(tmp_path, _api(measured=m))).endswith(
        "Input kinds were not measured for this run."
    )


@pytest.mark.parametrize(
    "split", [{"forecast": None, "observed": 6000}, {"forecast": 330, "observed": None}]
)
def test_a_null_count_in_the_input_split_is_not_measured_not_an_error(tmp_path, split):
    """A null count passed the `in` check and threw in toLocaleString, leaving no caption."""
    m = MEASURED | {"trades_by_input": split}
    assert _caption(render(tmp_path, _api(measured=m))).endswith(
        "Input kinds were not measured for this run."
    )
