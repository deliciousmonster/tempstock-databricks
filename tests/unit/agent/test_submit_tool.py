"""`submit_recommendation`: the handler refuses what the schema forbids, and the schema says so first."""

from datetime import UTC, datetime, timedelta

import pytest

from weather_signal.agent.tools import Session, get_prices, refusal, schemas, submit

OK = "Signal sell at score -0.42; anomaly -2.1 C and a 5-day return of -3.1%."
AS_OF = datetime(2026, 11, 18, 14, 30, tzinfo=UTC)
SELL = {"signal_action": "sell", "horizon_days": 0}
HOLD = {"signal_action": "hold", "horizon_days": 1}


def session():
    return Session(
        inputs={"2026-11-18": dict(SELL), "2026-11-19": dict(HOLD)},
        prices=[{"close": float(i)} for i in range(40)],
        as_of=AS_OF,
    )


def test_accepts_and_records():
    s = session()
    out = submit(s, {"target_date": "2026-11-18", **SELL, "downgrade": False, "rationale": OK})
    assert "is_error" not in out
    assert s.proposals["2026-11-18"].downgrade is False


@pytest.mark.parametrize(
    ("args", "why"),
    [
        ({"target_date": "2026-12-01", "downgrade": False, "rationale": OK}, "unknown target_date"),
        (
            {
                "target_date": "2026-11-18",
                **SELL,
                "downgrade": True,
                "reason": "llm_unavailable",
                "rationale": OK,
            },
            "reason must be",
        ),
        (
            {"target_date": "2026-11-18", **SELL, "downgrade": True, "rationale": OK},
            "needs a reason",
        ),
        (
            {
                "target_date": "2026-11-19",
                **HOLD,
                "downgrade": True,
                "reason": "stale_inputs",
                "rationale": OK,
            },
            "already hold",
        ),
        (
            {"target_date": "2026-11-18", **SELL, "downgrade": False, "rationale": "too short"},
            "rationale must be",
        ),
        (
            {"target_date": "2026-11-18", **SELL, "downgrade": False, "rationale": "x" * 601},
            "rationale must be",
        ),
        ({"target_date": "2026-11-18", **SELL, "downgrade": "yes", "rationale": OK}, "boolean"),
    ],
)
def test_refuses(args, why):
    s = session()
    out = submit(s, args)
    assert out["is_error"] is True
    assert why in out["content"][0]["text"]
    assert s.proposals == {}


def test_second_submit_for_a_date_refused():
    s = session()
    submit(s, {"target_date": "2026-11-18", **SELL, "downgrade": False, "rationale": OK})
    again = {
        "target_date": "2026-11-18",
        **SELL,
        "downgrade": True,
        "reason": "stale_inputs",
        "rationale": OK,
    }
    assert "one per date" in refusal(s, again)
    assert s.proposals["2026-11-18"].downgrade is False


def test_schema_carries_enums_and_bounds():
    sch = schemas(["2026-11-18"])["submit_recommendation"]
    props = sch["properties"]
    assert props["target_date"]["enum"] == ["2026-11-18"]
    assert props["reason"]["enum"] == ["stale_inputs", "out_of_range", "inconsistent_inputs"]
    assert (props["rationale"]["minLength"], props["rationale"]["maxLength"]) == (40, 600)
    assert "action" not in props
    assert schemas([])["get_prices"]["properties"]["days"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 30,
    }


@pytest.mark.parametrize("days", [0, 31, True, "5"])
def test_get_prices_bounds(days):
    assert get_prices(session(), {"days": days})["is_error"] is True


def _hour_of_2026_11_19():
    """The 11:30Z hour's two last dates, with the forecast past the bound for the buy date."""
    as_of = datetime(2026, 11, 19, 11, 30, tzinfo=UTC)
    buy = {
        "signal_action": "buy",
        "horizon_days": 5,
        "score": 0.581,
        "forecast_known_at": (as_of - timedelta(hours=31)).isoformat(),
        "inputs_max_known_at": (as_of - timedelta(hours=3)).isoformat(),
        "anomaly_c": 0.581,
        "index_c": 10.25,
        "ret_5d": 0.0134,
    }
    hold = {"signal_action": "hold", "horizon_days": 6, "score": 0.0, "forecast_known_at": None}
    return Session(inputs={"2026-11-24": buy, "2026-11-25": hold}, prices=[], as_of=as_of)


TEXT_25 = "Signal is hold with reason horizon_unavailable at horizon_days 6; score 0.0, no inputs."


def test_another_dates_facts_are_refused():
    """Dev row 2026-11-24 at 11:30Z: the 11-25 text and facts, kept as buy past the stale bound."""
    s = _hour_of_2026_11_19()
    submit(
        s,
        {
            "target_date": "2026-11-25",
            "signal_action": "hold",
            "horizon_days": 6,
            "downgrade": False,
            "rationale": TEXT_25,
        },
    )
    wrong = {
        "target_date": "2026-11-24",
        "signal_action": "hold",
        "horizon_days": 6,
        "downgrade": False,
        "rationale": TEXT_25,
    }
    assert "is not 2026-11-24's input" in refusal(s, wrong)
    echoed = {**wrong, "signal_action": "buy", "horizon_days": 5}
    assert "breach ['stale_inputs']" in refusal(s, echoed)
    held = {**echoed, "downgrade": True, "reason": "stale_inputs"}
    assert "already given for another date" in refusal(s, held)
    own = {**held, "rationale": "Held: forecast_known_at is 31 h before as_of; score 0.581."}
    assert refusal(s, own) is None


@pytest.mark.parametrize(
    ("kw", "why"),
    [
        ({"downgrade": True, "reason": "out_of_range"}, "out_of_range does not hold"),
        ({"horizon_days": 4}, "is not 2026-11-24's input"),
        ({"signal_action": None}, "is not 2026-11-24's input"),
    ],
)
def test_facts_must_be_the_dates_own(kw, why):
    s = _hour_of_2026_11_19()
    args = {
        "target_date": "2026-11-24",
        "signal_action": "buy",
        "horizon_days": 5,
        "downgrade": True,
        "reason": "stale_inputs",
        "rationale": "x" * 40,
        **kw,
    }
    assert why in refusal(s, args)


def test_schema_requires_the_echo():
    sch = schemas(["2026-11-18"])["submit_recommendation"]
    assert {"signal_action", "horizon_days"} <= set(sch["required"])


HOLD_TEXT = "Signal is hold: horizon unavailable, so no forecast, anomaly or return to explain."


def test_two_hold_dates_with_the_same_facts_may_share_text():
    """Identical text is refused only when the facts differ."""
    gone = {"signal_action": "hold", "score": 0.0, "reasons": ["horizon_unavailable"]}
    s = Session(
        inputs={"a": {**gone, "horizon_days": 6}, "b": {**gone, "horizon_days": 7}},
        prices=[],
        as_of=AS_OF,
    )
    for d, hz in (("a", 6), ("b", 7)):
        args = {
            "target_date": d,
            "signal_action": "hold",
            "horizon_days": hz,
            "downgrade": False,
            "rationale": HOLD_TEXT,
        }
        assert refusal(s, args) is None
        submit(s, args)
