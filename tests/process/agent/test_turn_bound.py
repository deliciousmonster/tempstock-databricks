"""A turn that never answers: bound at `turn_s`, the child reaped inside the live worker, no fallback."""

import json


def test_bound_then_closed(world):
    probe = world.root / "probe.jsonl"
    world.seams["turn_s"] = 2
    world.cue([{"turn": "hang"}])
    assert world.run(driver="child_probe", env={"CHILD_PROBE_OUT": str(probe)}).rc == 0
    row = world.run_row()
    assert (row["outcome"], row["route"], row["error"]) == (
        "llm_incomplete",
        "databricks-gateway",
        "turn_bound",
    )
    assert row["api_error_status"] is None
    assert world.spawns() == 1
    assert {r["downgrade_reason"] for r in world.recs()} == {"llm_incomplete"}
    (seen,) = [json.loads(x) for x in probe.read_text().splitlines()]
    assert seen["children"] == []
    assert seen["session_s"] < 2 + 6
