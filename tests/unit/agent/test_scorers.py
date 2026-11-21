"""The four code scorers and the prompt's thresholds at their boundaries."""

from datetime import UTC, datetime, time, timedelta

import pytest

from weather_signal import asof, config
from weather_signal.agent import options
from weather_signal.agent.scorers import (
    P_MIN_H,
    STALE,
    cites_numbers,
    downgrade_matches_fault,
    faults,
    no_contradiction,
    no_downgrade_clean,
)

AS_OF = datetime(2026, 11, 18, 14, 30, tzinfo=UTC)


def expected_fault(r, as_of):
    """The first reason `faults` gives, the one the prompt names; None for a clean row."""
    found = faults(r, as_of)
    return found[0] if found else None


def row(**kw):
    base = {
        "signal_action": "sell",
        "forecast_known_at": AS_OF - timedelta(hours=2),
        "inputs_max_known_at": AS_OF - timedelta(hours=1),
        "index_c": 3.1,
        "anomaly_c": -2.1,
        "ret_5d": -0.03,
    }
    return {**base, **kw}


@pytest.mark.parametrize(
    ("kw", "fault"),
    [
        ({}, None),
        ({"forecast_known_at": AS_OF - timedelta(hours=30)}, None),
        ({"forecast_known_at": AS_OF - timedelta(hours=30, seconds=1)}, "stale_inputs"),
        ({"anomaly_c": 12.0}, None),
        ({"anomaly_c": -12.5}, "out_of_range"),
        ({"index_c": 38.5}, "out_of_range"),
        ({"ret_5d": 0.25}, None),
        ({"ret_5d": 0.3}, "inconsistent_inputs"),
        ({"forecast_known_at": AS_OF - timedelta(minutes=30)}, "inconsistent_inputs"),
        ({"signal_action": "hold", "anomaly_c": 40}, None),
    ],
)
def test_expected_fault(kw, fault):
    assert expected_fault(row(**kw), AS_OF) == fault


def test_downgrade_matches_fault():
    assert downgrade_matches_fault(
        {"action": "hold", "downgrade_reason": "stale_inputs"}, "stale_inputs"
    )
    assert not downgrade_matches_fault(
        {"action": "hold", "downgrade_reason": "out_of_range"}, "stale_inputs"
    )
    assert not downgrade_matches_fault({"action": "sell", "downgrade_reason": None}, "stale_inputs")


def test_no_downgrade_clean():
    assert no_downgrade_clean({"action": "sell", "signal_action": "sell", "downgrade_reason": None})
    assert not no_downgrade_clean(
        {"action": "hold", "signal_action": "sell", "downgrade_reason": "out_of_range"}
    )


@pytest.mark.parametrize(
    ("text", "nums", "ok"),
    [
        ("Anomaly of -2.1 C drives a sell.", [-2.1], True),
        ("A 5-day return of -3.1% backs it.", [-0.031], True),
        ("Score -0.42 at close 187.25.", [187.25], True),
        ("Cold week ahead, so sell.", [-2.1, 187.25], False),
        ("Anomaly 2.4 C.", [-2.1], False),
    ],
)
def test_cites_numbers(text, nums, ok):
    assert cites_numbers(text, nums) is ok


@pytest.mark.parametrize(
    ("text", "action", "ok"),
    [
        ("Held: the forecast is stale. A buy would be premature.", "hold", False),
        ("Cold anomaly supports the sell. No reason to buy.", "sell", False),
        ("Cold anomaly supports selling.", "sell", True),
        ("Inputs are stale, so held.", "hold", True),
        ("The signal says buy and the anomaly agrees.", "buy", True),
    ],
)
def test_no_contradiction(text, action, ok):
    assert no_contradiction(text, action) is ok


def _signal_known_at(as_of: datetime, p_hours: int, horizon: int) -> datetime:
    """The `forecast_known_at` the signal carries: its fitted lead's last hour of the target."""
    day = asof.decision_day(as_of, p_hours)
    last_hour = datetime.combine(asof.target_date(day, horizon), time(23), tzinfo=UTC)
    return asof.known_at_forecast(last_hour, asof.fitted_lead(horizon), p_hours)


@pytest.mark.parametrize("p_hours", [19, 25, 30, 36])
def test_daily_forecast_is_not_stale_at_any_hour(p_hours):
    start = datetime(2026, 11, 19, 0, 30, tzinfo=UTC)
    for as_of in (start + timedelta(hours=i) for i in range(48)):
        for h in asof.horizons(as_of, p_hours):
            fk = _signal_known_at(as_of, p_hours, h)
            r = row(forecast_known_at=fk, inputs_max_known_at=as_of)
            assert fk <= as_of
            assert expected_fault(r, as_of) != "stale_inputs", (as_of, h, fk)


def test_scorer_flags_a_row_a_day_behind():
    """The scorer alone: a missed daily step never reaches the agent, since the model holds it."""
    as_of = datetime(2026, 11, 19, 12, 30, tzinfo=UTC)
    fk = _signal_known_at(as_of, 30, 0) - timedelta(days=1)
    assert expected_fault(row(forecast_known_at=fk), as_of) == "stale_inputs"


def test_prompt_states_the_scorer_bound():
    hours = int(STALE.total_seconds() // 3600)
    text = options.prompt_text()
    assert f"`as_of` minus {hours} hours (exactly {hours} hours" in " ".join(text.split())


def test_p_hours_keeps_the_bound_sound():
    assert config.P_PROVISIONAL_H >= P_MIN_H
    test_daily_forecast_is_not_stale_at_any_hour(P_MIN_H)


def test_faults_lists_every_breach_and_tolerates_nulls():
    r = row(forecast_known_at=AS_OF - timedelta(hours=31), anomaly_c=13.0, ret_5d=0.3)
    assert faults(r, AS_OF) == ("stale_inputs", "out_of_range", "inconsistent_inputs")
    assert faults(row(forecast_known_at=None, inputs_max_known_at=None), AS_OF) == ()


def test_any_breached_reason_scores_as_a_match():
    """The tool accepts any breached reason, so the eval must too."""
    from weather_signal.agent import evaluate as run_eval

    src = row(forecast_known_at=AS_OF - timedelta(hours=30, minutes=30), anomaly_c=13.0)
    assert faults(src, AS_OF) == ("stale_inputs", "out_of_range")
    written = {
        "target_date": datetime(2026, 11, 19).date(),
        "action": "hold",
        "signal_action": "sell",
        "downgrade_reason": "out_of_range",
        "rationale": "Held: anomaly_c 13.0 is out of range.",
    }
    hour = {"inputs": {"2026-11-19": src}}
    assert run_eval.score_run("out_of_range", 0, AS_OF, hour, [written]).downgrade_ok
