"""The MERGE is the foundation's, sourced from `signals.signal`; Python binds no action and no signal."""

from datetime import UTC, date, datetime

import pytest

from tests.integration.helpers import AGENT_MERGE
from weather_signal.agent.merge import bind, merge_sql
from weather_signal.agent.proposal import MergeParams
from weather_signal.agent.route import ROUTES

RECS, SIGNAL = "workspace.dev_signals.recommendations", "workspace.dev_signals.signal"
META = {"llm_model_id": "claude-haiku-4-5", "trace_id": "tr-1", "agent_version": "0.1.0"}
NAMES = {
    "as_of",
    "target_date",
    "downgrade",
    "downgrade_reason",
    "rationale",
    "llm_model_id",
    "llm_route",
    "trace_id",
    "agent_version",
}


def test_text_is_the_foundations():
    assert merge_sql(RECS, SIGNAL, llm_route=True) == AGENT_MERGE.format(recs=RECS, signal=SIGNAL)
    assert f"FROM {SIGNAL} s" in merge_sql(RECS, SIGNAL, llm_route=True)


@pytest.mark.parametrize("route", [True, False])
def test_confidence_is_the_signals_not_bound(route):
    """Change 11: no `:confidence` marker and no LEAST over a bound value."""
    sql = merge_sql(RECS, SIGNAL, llm_route=route)
    assert ":confidence" not in sql
    assert "LEAST" not in sql.upper()
    assert "s.confidence AS confidence" in sql


def test_route_column_adds_one_marker_and_no_source_change():
    with_route = merge_sql(RECS, SIGNAL, llm_route=True)
    assert with_route.count(":llm_route") == 1
    assert "s.action AS signal_action" in with_route
    assert "VALUES" not in with_route.upper()


@pytest.mark.parametrize("route", ROUTES)
def test_bound_names(route):
    p = MergeParams("2026-11-19", True, "stale_inputs", "r" * 40)
    out = bind(
        datetime(2026, 11, 18, 14, 30, tzinfo=UTC),
        date(2026, 11, 19),
        p,
        meta=META,
        llm_route=route,
    )
    assert set(out) == NAMES
    assert not set(out) & {"action", "signal_action"}
    assert out["llm_route"] == route
    assert route in {"databricks-gateway", "direct", "direct_fallback", "none"}


def test_markers_in_text_match_bound_names():
    import re

    markers = set(re.findall(r":([a-z_]+)", merge_sql(RECS, SIGNAL, llm_route=True)))
    assert markers == NAMES


def test_eval_target_and_refusal():
    sql = merge_sql(
        "workspace.dev_signals.recommendations_eval", SIGNAL, llm_route=True, eval_run=True
    )
    assert "r.eval_run_id = src.eval_run_id" in sql
    with pytest.raises(ValueError, match="not an allowed MERGE target"):
        merge_sql("workspace.dev_signals.signal", SIGNAL, llm_route=False)


def test_values_for_another_date_are_refused():
    p = MergeParams("2026-11-25", False, None, "r" * 40)
    with pytest.raises(ValueError, match="2026-11-25 bound to 2026-11-24"):
        bind(
            datetime(2026, 11, 19, 11, 30, tzinfo=UTC),
            date(2026, 11, 24),
            p,
            meta=META,
            llm_route=None,
        )
