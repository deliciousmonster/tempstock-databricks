"""The three in-process tools, their JSON Schemas, and the PreToolUse hook that denies everything else."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from weather_signal.agent.proposal import MODEL_REASONS, Proposal
from weather_signal.agent.scorers import ACTIONS, faults
from weather_signal.config import PRIMARY

log = logging.getLogger(__name__)

SERVER = "ws"
NAMES = ("get_inputs", "get_prices", "submit_recommendation")
ALLOWED = tuple(f"mcp__{SERVER}__{n}" for n in NAMES)
RATIONALE_MIN, RATIONALE_MAX = 40, 600
PRICE_DAYS_MAX = 30
PRICES_DOC = f"The last `days` {PRIMARY} daily closes."


@dataclass
class Session:
    """One hour's inputs, served from memory, and the proposals accepted so far."""

    inputs: dict[str, dict[str, Any]]
    prices: list[dict[str, Any]]
    as_of: datetime
    proposals: dict[str, Proposal] = field(default_factory=dict)
    denials: int = 0

    @property
    def dates(self) -> list[str]:
        return sorted(self.inputs)


def schemas(dates: list[str]) -> dict[str, dict[str, Any]]:
    date = {"type": "string", "enum": dates}
    return {
        "get_inputs": {
            "type": "object",
            "properties": {"target_date": date},
            "required": ["target_date"],
            "additionalProperties": False,
        },
        "get_prices": {
            "type": "object",
            "properties": {"days": {"type": "integer", "minimum": 1, "maximum": PRICE_DAYS_MAX}},
            "required": ["days"],
            "additionalProperties": False,
        },
        "submit_recommendation": {
            "type": "object",
            "properties": {
                "target_date": date,
                "signal_action": {"type": "string", "enum": list(ACTIONS)},
                "horizon_days": {"type": "integer"},
                "downgrade": {"type": "boolean"},
                "reason": {"type": "string", "enum": list(MODEL_REASONS)},
                "rationale": {
                    "type": "string",
                    "minLength": RATIONALE_MIN,
                    "maxLength": RATIONALE_MAX,
                },
            },
            "required": ["target_date", "signal_action", "horizon_days", "downgrade", "rationale"],
            "additionalProperties": False,
        },
    }


def _text(obj: Any, *, error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(obj, default=str)}]}
    if error:
        out["is_error"] = True
    return out


def get_inputs(s: Session, args: dict[str, Any]) -> dict[str, Any]:
    row = s.inputs.get(str(args.get("target_date")))
    if row is None:
        return _text({"error": f"unknown target_date; one of {s.dates}"}, error=True)
    return _text(row)


def get_prices(s: Session, args: dict[str, Any]) -> dict[str, Any]:
    days = args.get("days")
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= PRICE_DAYS_MAX:
        return _text({"error": f"days must be an integer from 1 to {PRICE_DAYS_MAX}"}, error=True)
    return _text(s.prices[-days:])


def refusal(s: Session, args: dict[str, Any]) -> str | None:
    """Why a submission is refused, or `None` when it is accepted; the first failed check wins."""
    return _shape(s, args) or _facts(s, args)


def _shape(s: Session, args: dict[str, Any]) -> str | None:
    d, down, reason, text = (
        args.get(k) for k in ("target_date", "downgrade", "reason", "rationale")
    )
    checks = (
        (lambda: d not in s.inputs, f"unknown target_date; one of {s.dates}"),
        (lambda: d in s.proposals, f"{d} already has a recommendation; one per date"),
        (lambda: not isinstance(down, bool), "downgrade must be a boolean"),
        (
            lambda: reason is not None and reason not in MODEL_REASONS,
            f"reason must be one of {list(MODEL_REASONS)}",
        ),
        (lambda: bool(down) and reason is None, "a downgrade needs a reason"),
        (
            lambda: not isinstance(text, str) or not RATIONALE_MIN <= len(text) <= RATIONALE_MAX,
            f"rationale must be {RATIONALE_MIN} to {RATIONALE_MAX} characters",
        ),
    )
    return next((why for failed, why in checks if failed()), None)


def _facts_of(row: dict[str, Any]) -> dict[str, Any]:
    """A date's inputs less its position in the outlook, which shared text may leave unsaid."""
    return {k: v for k, v in row.items() if k != "horizon_days"}


def _facts(s: Session, args: dict[str, Any]) -> str | None:
    """The submission against its own date's inputs: the echoed facts, the thresholds, the text."""
    d = str(args["target_date"])
    row = s.inputs[d]
    echo = {k: args.get(k) for k in ("signal_action", "horizon_days")}
    if echo != {k: row.get(k) for k in echo}:
        return f"{echo} is not {d}'s input; call get_inputs for {d} and describe that date"
    if args["downgrade"] and row.get("signal_action") == "hold":
        return "the signal is already hold; nothing to downgrade"
    found = faults(row, s.as_of)
    if args["downgrade"] and args.get("reason") not in found:
        return f"{args.get('reason')} does not hold for {d}'s inputs"
    if not args["downgrade"] and found:
        return f"{d}'s inputs breach {list(found)}; downgrade with one of them"
    if any(
        p.rationale == args["rationale"] and _facts_of(s.inputs[p.target_date]) != _facts_of(row)
        for p in s.proposals.values()
    ):
        return "that rationale was already given for another date; write this date's own"
    return None


def submit(s: Session, args: dict[str, Any]) -> dict[str, Any]:
    why = refusal(s, args)
    if why is not None:
        return _text({"error": why}, error=True)
    down = args["downgrade"]
    d = args["target_date"]
    s.proposals[d] = Proposal(d, down, args.get("reason") if down else None, args["rationale"])
    return _text({"accepted": d})


async def deny_outside_three(
    hook_input: dict[str, Any], _tool_use_id: str | None, _ctx: Any, *, session: Session
) -> dict[str, Any]:
    name = hook_input.get("tool_name")
    if name in ALLOWED:
        return {}
    session.denials += 1
    log.warning("hook denied tool %s", name)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "only the three weather-signal tools are available",
        }
    }


def server(s: Session) -> Any:
    """The `ws` SDK MCP server over `s`; the read tools are annotated read-only."""
    from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

    sch = schemas(s.dates)
    ro = ToolAnnotations(readOnlyHint=True)

    @tool(
        "get_inputs",
        "Signal, forecast and price inputs for one target date.",
        sch["get_inputs"],
        ro,
    )
    async def _inputs(args: dict[str, Any]) -> dict[str, Any]:
        return get_inputs(s, args)

    @tool("get_prices", PRICES_DOC, sch["get_prices"], ro)
    async def _prices(args: dict[str, Any]) -> dict[str, Any]:
        return get_prices(s, args)

    @tool(
        "submit_recommendation",
        "Submit one date's explanation, echoing its signal_action and horizon_days from "
        "get_inputs, optionally downgrading it to hold for a named reason.",
        sch["submit_recommendation"],
    )
    async def _submit(args: dict[str, Any]) -> dict[str, Any]:
        return submit(s, args)

    return create_sdk_mcp_server(SERVER, "1.0.0", tools=[_inputs, _prices, _submit])
