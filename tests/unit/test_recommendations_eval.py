"""Setup creates `signals.recommendations_eval`, and run_eval checks that schema."""

import re
from pathlib import Path

from tests.unit.guard_world import N, world
from weather_signal.agent.merge import merge_sql
from weather_signal.catalog import setup

EVAL = N.table("signals", "recommendations_eval")
RUN_EVAL = Path(__file__).parents[2] / "src/weather_signal/agent/evaluate.py"


def test_setup_creates_the_eval_table_with_every_column_the_eval_merge_inserts():
    cat = world()
    assert EVAL in cat.tables
    create = next(s for s in cat.log if s.startswith("CREATE TABLE") and EVAL in s)
    cols = {c.strip().split()[0] for c in create[create.index("(") + 1 :].split(", ")}
    q = merge_sql(EVAL, N.table("signals", "signal"), llm_route=True, eval_run=True)
    src = {"as_of", "target_date", "horizon_days", *re.findall(r"AS (\w+)", q)}
    assert src <= cols, src - cols


def test_the_eval_table_keeps_the_never_flip_check_and_a_rerun_adds_nothing():
    cat = world()
    assert cat.properties(EVAL)["delta.constraints.never_flip"] == (
        "action IN (signal_action, 'hold')"
    )
    v = cat.latest_version(EVAL)
    setup.run(cat, N)
    assert cat.latest_version(EVAL) == v


def test_run_eval_names_the_eval_table_in_the_signals_schema_only():
    text = RUN_EVAL.read_text(encoding="utf-8")
    schemas = re.findall(r"n\.table\([\"'](\w+)[\"'], [\"']recommendations_eval", text)
    assert schemas and set(schemas) == {"signals"}
    assert "dev_signals.recommendations_eval" in text
