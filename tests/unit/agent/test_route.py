"""`wants_fallback` and `classify`: direct only on crewkit's gateway pre-check; a sent prompt's outcome stands."""

import pytest

pytest.importorskip("crewkit.databricks_gateway")

from crewkit.agent_sdk import Refused, Unavailable
from crewkit.databricks_gateway import DatabricksGatewayUnavailable

from weather_signal.agent.route import Outcome, Turn, classify, wants_fallback

PRE_CHECK = [
    "databricks_gateway_token",
    "databricks_gateway_token_short",
    "databricks_gateway_not_relayed",
    "databricks_gateway_service_unknown",
    "databricks_gateway_auth",
    "databricks_gateway_unreachable",
]


@pytest.mark.parametrize("code", PRE_CHECK)
def test_each_pre_check_goes_direct(code):
    assert wants_fallback(DatabricksGatewayUnavailable(code)) is True


@pytest.mark.parametrize(
    "err",
    [
        Refused("token_present"),
        Unavailable("connect_timeout"),
        Unavailable("cli_failed:ProcessError"),
        TimeoutError("turn_bound"),
        RuntimeError("ProcessError"),
    ],
)
def test_everything_else_holds(err):
    assert wants_fallback(err) is False


STATUSES = [401, 403, 404, 429, 500, 502, 503, 504, 529]


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("submitted", [0, 3])
def test_a_status_after_the_prompt_keeps_the_gateway(status, submitted):
    o = Outcome(
        "turn", "databricks-gateway", turn=Turn("error", api_error_status=status, subtype="success")
    )
    outcome, missing = classify(o, 7, submitted)
    assert o.route == "databricks-gateway" and o.fallback_reason is None
    assert o.turn.api_error_status == status
    assert (outcome, missing) == (
        ("llm_unavailable", "llm_unavailable") if submitted == 0 else ("partial", "llm_incomplete")
    )


def test_bound_expiry_records_no_status():
    o = Outcome("turn", "databricks-gateway", turn=Turn("bound", error="turn_bound"))
    assert classify(o, 7, 0) == ("llm_incomplete", "llm_incomplete")
    assert o.turn.api_error_status is None  # the CLI returned nothing inside its backoff


def test_fallback_records_route_and_reason():
    o = Outcome("turn", "direct_fallback", turn=Turn("ok")).as_fallback(
        "databricks_gateway_not_relayed"
    )
    assert (o.route, o.fallback_reason) == ("direct_fallback", "databricks_gateway_not_relayed")
    none = Outcome("budget", "none", error="budget").as_fallback("databricks_gateway_token")
    assert none.route == "none"


def _fake_connect(monkeypatch, verdict_route):
    import contextlib
    import types

    import crewkit.agent_sdk as sdk

    from weather_signal.agent import route

    @contextlib.asynccontextmanager
    async def connect(options, **kw):
        yield object(), types.SimpleNamespace(route=verdict_route, backend="plan")

    async def run_turn(client, prompt, turn_s, *, backend):
        return Turn("ok")

    monkeypatch.setattr(sdk, "connect", connect)
    monkeypatch.setattr(route, "run_turn", run_turn)
    return route


@pytest.mark.parametrize(
    ("verdict", "taken"), [("direct", "direct"), ("databricks-gateway", "databricks-gateway")]
)
def test_first_attempt_keeps_the_route_crewkit_took(monkeypatch, verdict, taken):
    """A first attempt that went direct is `direct`, never a fallback with no reason."""
    import anyio

    route = _fake_connect(monkeypatch, verdict)
    out = anyio.run(lambda: route._session(None, "p", 5.0, key_file=None, route=None))
    assert (out.route, out.fallback_reason) == (taken, None)
    assert out.route != "direct_fallback"


@pytest.mark.parametrize(
    "turn", [Turn("bound", error="turn_bound"), Turn("error", subtype="error_max_turns")]
)
def test_every_date_submitted_is_ok_whatever_ended_the_turn(turn):
    """All seven rows carry a model rationale, so the hour is not `partial`."""
    assert classify(Outcome("turn", "databricks-gateway", turn=turn), 7, 7) == (
        "ok",
        "llm_incomplete",
    )
