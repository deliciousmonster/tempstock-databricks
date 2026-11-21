"""Recorded CLI 2.1.326 frames: they parse, and an error result maps to `llm_unavailable`.

`apiKeySource` is `none` on the bearer and Bedrock inits, as on the subscription, so the gate is crewkit's
account check and never this field. Re-record with `tests/fixtures/agent/frames/record.py`.
"""

import json
from pathlib import Path

import pytest

pytest.importorskip("claude_agent_sdk")

from claude_agent_sdk import ResultMessage, SystemMessage
from claude_agent_sdk._internal.message_parser import parse_message
from crewkit.agent_sdk import check

from weather_signal.agent.route import Outcome, _result, classify

FRAMES = Path(__file__).resolve().parents[2] / "fixtures" / "agent" / "frames"
ACCOUNTS = FRAMES.parent / "accounts"


def frames(name):
    return json.loads((FRAMES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["bearer", "bedrock", "api_key"])
def test_init_and_result_parse(name):
    d = frames(name)
    init = parse_message({"type": "system", **d["init"]})
    assert isinstance(init, SystemMessage) and init.subtype == "init"
    assert init.data["claude_code_version"] == "2.1.326"
    res = parse_message(
        {"type": "result", **{k: v for k, v in d["result"].items() if v is not None}}
    )
    assert isinstance(res, ResultMessage)


@pytest.mark.parametrize("name", ["bearer", "bedrock"])
def test_api_key_source_is_none_off_the_subscription(name):
    assert frames(name)["init"]["apiKeySource"] == "none"


def test_the_gate_is_the_account_not_the_init_frame():
    bearer = json.loads((ACCOUNTS / "bearer.json").read_text(encoding="utf-8"))
    sub = json.loads((ACCOUNTS / "subscription.json").read_text(encoding="utf-8"))
    assert check(bearer, "plan", "2.1.326").reason == "token_present"
    assert check(sub, "plan", "2.1.326").allowed is True


@pytest.mark.parametrize("name", ["bearer", "cli-result-503"])
def test_error_result_maps_to_llm_unavailable(name):
    d = frames(name)
    raw = d["result"] if isinstance(d.get("result"), dict) else d
    m = parse_message({"type": "result", **{k: v for k, v in raw.items() if k != "type"}})
    t = _result(m, {})
    assert (t.status, t.api_error_status) == ("error", 503)
    assert classify(Outcome("turn", "databricks-gateway", turn=t), 7, 0) == (
        "llm_unavailable",
        "llm_unavailable",
    )
