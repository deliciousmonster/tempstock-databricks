"""The crewkit call: the gateway route, direct only when a gateway pre-check failed before any child started."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from weather_signal.agent.budget import (
    DIRECT_CAP_S,
    DIRECT_PRE_S,
    GATEWAY_CAP_S,
    GATEWAY_PRE_S,
    Budget,
)
from weather_signal.catalog.ddl import LLM_ROUTES

log = logging.getLogger(__name__)

ROUTES = LLM_ROUTES
CONNECT_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class Turn:
    status: str  # ok, error, bound, process, mismatch
    api_error_status: int | None = None
    error: str | None = None
    subtype: str | None = None
    model: str | None = None
    api_key_source: str | None = None
    duration_ms: int | None = None
    duration_api_ms: int | None = None
    num_turns: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class Outcome:
    kind: str  # turn, refused, unavailable, budget
    route: str
    turn: Turn | None = None
    verdict: Any = None
    fallback_reason: str | None = None
    refusal: str | None = None
    error: str | None = None

    def as_fallback(self, reason: str) -> Outcome:
        route = "direct_fallback" if self.kind == "turn" else "none"
        return replace(self, route=route, fallback_reason=reason)


def wants_fallback(err: BaseException) -> bool:
    """Go direct only on crewkit's gateway pre-check failure, raised before any child starts."""
    from crewkit.databricks_gateway import DatabricksGatewayUnavailable

    return isinstance(err, DatabricksGatewayUnavailable)


def error_text(err: BaseException) -> str:
    """Class and first line only: never a repr, argv or chained cause."""
    first = (str(err).splitlines() or [""])[0][:200]
    return f"{type(err).__name__}: {first}" if first else type(err).__name__


def classify(o: Outcome, n_dates: int, n_submitted: int) -> tuple[str, str]:
    """The run's outcome and the reason bound on every date without a proposal."""
    if o.kind == "refused":
        return "auth_refused", "llm_unavailable"
    if o.kind != "turn" or o.turn is None:
        return "llm_unavailable", "llm_unavailable"
    status = o.turn.status
    missing = "llm_incomplete"
    if status == "process" or (status == "error" and o.turn.subtype == "success"):
        missing = "llm_unavailable" if n_submitted == 0 else "llm_incomplete"
    if n_submitted == n_dates:  # every row carries a model rationale; `error` keeps the turn's end
        return "ok", missing
    if n_submitted > 0:
        return "partial", missing
    return missing, missing


async def run_turn(client: Any, prompt: str, turn_s: float, *, backend: str) -> Turn:
    import anyio
    from claude_agent_sdk import CLIConnectionError, ProcessError, ResultMessage, SystemMessage

    seen: dict[str, Any] = {}
    result = None
    try:
        with anyio.fail_after(turn_s):
            await client.query(prompt)
            async for m in client.receive_response():  # ends after the result, so autolog runs
                if isinstance(m, SystemMessage) and m.subtype == "init":
                    seen["model"] = m.data.get("model")
                    seen["api_key_source"] = m.data.get("apiKeySource")
                    if backend == "plan" and seen["api_key_source"] not in (None, "none"):
                        return Turn(
                            "mismatch", error=f"apiKeySource:{seen['api_key_source']}", **seen
                        )
                if isinstance(m, ResultMessage):
                    result = m
    except TimeoutError:
        return Turn("bound", error="turn_bound", **seen)
    except (ProcessError, CLIConnectionError) as err:
        return Turn("process", error=error_text(err), **seen)
    return (
        _result(result, seen) if result is not None else Turn("process", error="no_result", **seen)
    )


def _result(m: Any, seen: dict[str, Any]) -> Turn:
    bad = m.is_error or m.subtype != "success"
    first = (m.errors or [m.result or ""])[0] if bad else None
    return Turn(
        "error" if bad else "ok",
        api_error_status=m.api_error_status,
        error=f"{m.subtype}: {str(first)[:200]}" if bad else None,
        subtype=m.subtype,
        duration_ms=m.duration_ms,
        duration_api_ms=m.duration_api_ms,
        num_turns=m.num_turns,
        cost_usd=m.total_cost_usd,
        **seen,
    )


async def _session(
    options: Any, prompt: str, turn_s: float, *, key_file: Path | None, route: Any
) -> Outcome:
    from crewkit.agent_sdk import connect

    # Every thread alive here sees crewkit's reduced os.environ until connect returns.
    log.info("env window opens; threads=%s", sorted(t.name for t in threading.enumerate()))
    async with connect(options, key_file=key_file, timeout_s=CONNECT_TIMEOUT_S, route=route) as (
        client,
        verdict,
    ):
        log.info("connected route=%s", verdict.route)
        t = await run_turn(client, prompt, turn_s, backend=verdict.backend)
    taken = "databricks-gateway" if verdict.route == "databricks-gateway" else "direct"
    return Outcome("turn", taken, turn=t, verdict=verdict)


async def explain(
    options: Any,
    prompt: str,
    budget: Budget,
    *,
    caps: tuple[float, float] = (GATEWAY_CAP_S, DIRECT_CAP_S),
    key_file: Path | None = None,
) -> Outcome:
    """One Claude turn on the configured route; direct only after a gateway pre-check failed."""
    from crewkit.agent_sdk import Refused, Unavailable

    turn_s = budget.turn_s(pre=GATEWAY_PRE_S, cap=caps[0])
    if turn_s is None:
        return Outcome("budget", "none", error="budget")
    reason = None
    try:
        return await _session(options, prompt, turn_s, key_file=key_file, route=None)
    except Refused as err:
        return Outcome("refused", "none", verdict=err.verdict, refusal=err.reason)
    except Unavailable as err:
        if not wants_fallback(err):
            return Outcome("unavailable", "none", error=err.reason)
        reason = err.reason
    log.warning("gateway pre-check failed (%s); going direct", reason)
    turn_s = budget.turn_s(pre=DIRECT_PRE_S, cap=caps[1])
    if turn_s is None:
        return Outcome("budget", "none", fallback_reason=reason, error="budget")
    try:
        out = await _session(options, prompt, turn_s, key_file=key_file, route="direct")
    except Refused as err:
        out = Outcome("refused", "none", verdict=err.verdict, refusal=err.reason)
    except Unavailable as err:
        out = Outcome("unavailable", "none", error=err.reason)
    return out.as_fallback(reason)
