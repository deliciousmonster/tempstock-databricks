"""Intraday bars are display only: the backtest, train, score and the gate never read them."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from weather_signal.ingest import intraday, price_bars
from weather_signal.serving import views

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "weather_signal"
PIPELINE = ROOT / "src" / "weather_signal_etl" / "transformations" / "pipeline.py"
# Every model path: features, fit, gate, backtest, score, the guard and the agent that reads calls.
MODEL = sorted(
    [
        *(SRC / "signal").glob("*.py"),
        *(SRC / "guard").glob("*.py"),
        *(SRC / "agent").glob("*.py"),
        SRC / "jobs" / "backtest.py",
        SRC / "jobs" / "train.py",
        SRC / "jobs" / "score.py",
        SRC / "asof.py",
    ]
)
WORD = re.compile(r"intraday|yahoo_chart_60m", re.I)


@pytest.mark.parametrize("path", MODEL, ids=lambda p: str(p.relative_to(SRC)))
def test_no_model_module_names_intraday(path):
    assert not WORD.search(path.read_text(encoding="utf-8"))


def test_the_model_list_is_not_empty_and_holds_the_gate():
    names = {p.name for p in MODEL}
    assert {"backtest.py", "train.py", "score.py", "backtest_fit.py", "model.py"} <= names


def test_prices_raw_never_parses_an_intraday_envelope():
    """`prices_raw` keeps only `alphavantage` and `yahoo`; the parser returns nothing for this one."""
    assert price_bars.from_envelope(intraday.SOURCE, "u", {}, '{"chart": {}}') == []
    src = PIPELINE.read_text(encoding="utf-8")
    assert 'F.col("source").isin("alphavantage", "yahoo")' in src


def _readers(src: str) -> dict[str, set[str]]:
    """Per pipeline function, the names and strings it uses."""
    out: dict[str, set[str]] = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef):
            used = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            used |= {
                n.value
                for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            }
            used |= {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            out[node.name] = used
    return out


def test_only_the_intraday_table_reads_the_intraday_envelopes_or_the_table():
    readers = _readers(PIPELINE.read_text(encoding="utf-8"))
    touching = {
        f for f, used in readers.items() if {"SOURCE", "prices_intraday", "_intraday_bars"} & used
    }
    assert touching == {"prices_intraday"}, touching


def test_no_view_but_the_intraday_one_reads_the_table():
    from weather_signal.config import Names

    n = Names("workspace", "dev_")
    bodies = {**views.selects(n), views.INTRADAY_VIEW: views.intraday(n)}
    assert {k for k, v in bodies.items() if "prices_intraday" in v} == {views.INTRADAY_VIEW}
    assert views.INTRADAY_VIEW not in views.VIEWS  # so neither Genie nor the MCP gate offers it
