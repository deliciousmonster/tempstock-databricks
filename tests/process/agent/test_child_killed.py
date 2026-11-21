"""The CLI child killed mid-turn or before the query: `llm_unavailable`, no zombie, no fallback."""

import json

import pytest


@pytest.mark.parametrize(
    "cue", [{"kill_after": 2}, {"after_init": "kill"}], ids=["mid_turn", "before_query"]
)
def test_killed_child(world, cue):
    probe = world.root / "probe.jsonl"
    world.cue([cue])
    assert world.run(driver="child_probe", env={"CHILD_PROBE_OUT": str(probe)}).rc == 0
    row = world.run_row()
    assert row["route"] in ("databricks-gateway", "none")
    assert row["outcome"] in ("llm_unavailable", "partial")
    assert world.spawns() == 1
    recs = world.recs()
    assert len(recs) == 7
    assert {r["downgrade_reason"] for r in recs if r["downgrade"]} <= {
        "llm_unavailable",
        "llm_incomplete",
    }
    if row["outcome"] == "llm_unavailable":
        assert all(r["downgrade"] and r["downgrade_reason"] == "llm_unavailable" for r in recs)
    for line in probe.read_text().splitlines():
        assert json.loads(line)["children"] == []
