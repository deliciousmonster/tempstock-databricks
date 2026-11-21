"""Seven dates, five proposals: the two left are held with a code reason, never left as the signal."""

from weather_signal.agent.proposal import Proposal, fill
from weather_signal.agent.route import Outcome, Turn, classify

DATES = [f"2026-11-{d}" for d in range(18, 25)]
FIVE = {d: Proposal(d, False, None, "r" * 40) for d in DATES[:5]}


def test_two_missing_are_incomplete_downgrades():
    out = fill(DATES, FIVE, missing="llm_incomplete")
    assert [d for d, p in out.items() if p.downgrade] == DATES[5:]
    assert {out[d].downgrade_reason for d in DATES[5:]} == {"llm_incomplete"}


def test_budget_cap_keeps_the_five():
    o = Outcome("turn", "databricks-gateway", turn=Turn("error", subtype="error_max_budget_usd"))
    outcome, missing = classify(o, 7, 5)
    assert (outcome, missing) == ("partial", "llm_incomplete")
    assert sum(not p.downgrade for p in fill(DATES, FIVE, missing=missing).values()) == 5


def test_usage_limit_with_nothing_submitted_is_unavailable():
    o = Outcome(
        "turn", "databricks-gateway", turn=Turn("error", subtype="success", api_error_status=429)
    )
    assert classify(o, 7, 0) == ("llm_unavailable", "llm_unavailable")


def test_refusal_and_no_turn():
    assert classify(Outcome("refused", "none", refusal="token_present"), 7, 0)[0] == "auth_refused"
    assert classify(Outcome("budget", "none", error="budget"), 7, 0) == (
        "llm_unavailable",
        "llm_unavailable",
    )


def test_all_submitted_is_ok():
    every = {d: Proposal(d, False, None, "r" * 40) for d in DATES}
    assert (
        classify(Outcome("turn", "databricks-gateway", turn=Turn("ok")), 7, len(every))[0] == "ok"
    )
