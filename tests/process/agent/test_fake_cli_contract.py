"""The fake CLI answers initialize with every key the SDK and crewkit read, and runs the agent's hook."""

import importlib.util
import json
import os

from tests.process.agent.harness import FAKE, FIXTURES

READ_BY_SDK_OR_GATE = {"account", "claude_code_version", "hook_on_failure_supported"}


def fake_module(tmp_path, monkeypatch):
    monkeypatch.setitem(os.environ, "FAKE_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("fakecli", FAKE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_initialize_keys(tmp_path, monkeypatch):
    recorded = set(json.loads((FIXTURES / "accounts" / "initialize.json").read_text())["keys"])
    assert READ_BY_SDK_OR_GATE <= recorded
    answer = fake_module(tmp_path, monkeypatch).initialize_answer({})
    assert READ_BY_SDK_OR_GATE <= set(answer)
    assert answer["hook_on_failure_supported"] is True
    assert answer["claude_code_version"] == "2.1.326"


def test_connect_with_the_hook_and_probe_it(world):
    world.cue([{"hook_probe": ["Bash", "Skill", "mcp__other__x", "mcp__ws__get_inputs"]}])
    assert world.run().rc == 0
    decisions = {e["hook"]: e["decision"] for e in world.fake() if "hook" in e}
    assert decisions == {
        "Bash": "deny",
        "Skill": "deny",
        "mcp__other__x": "deny",
        "mcp__ws__get_inputs": "allow",
    }
    row = world.run_row()
    assert (row["outcome"], row["hook_denials"]) == ("ok", 3)
