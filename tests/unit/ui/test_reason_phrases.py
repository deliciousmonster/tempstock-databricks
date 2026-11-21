"""Every hold reason the model writes has a plain phrase in the page (open UI item, signal.reasons)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from weather_signal.signal import backtest

ROOT = Path(__file__).resolve().parents[3]


def model_codes() -> set[str]:
    tree = ast.parse((ROOT / "src/weather_signal/signal/model.py").read_text(encoding="utf-8"))
    fns = [
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name in ("freshness", "_forecast_reason")
    ]
    assert len(fns) == 2
    return {
        n.value
        for f in fns
        for n in ast.walk(f)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and re.fullmatch(r"[a-z]+(_[a-z]+)+", n.value)
    }


def page_phrases() -> dict[str, str]:
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    block = js[js.index("var REASONS = {") : js.index("};", js.index("var REASONS = {"))]
    return dict(re.findall(r'(\w+): "([^"]+)"', block))


def test_the_backtest_stale_reason_has_a_phrase():
    """A `stale_input` hold read as the raw code."""
    assert page_phrases()[backtest.STALE_INPUT] == "observed weather over 14 days old"


def test_every_model_reason_has_a_phrase():
    codes = model_codes()
    assert codes >= {"no_normal", "stale_forecast", "forecast_quarantined", "stale_prices"}
    phrases = page_phrases()
    assert codes <= set(phrases), codes - set(phrases)
    assert all("_" not in p and len(p) <= 40 for p in phrases.values()), phrases
