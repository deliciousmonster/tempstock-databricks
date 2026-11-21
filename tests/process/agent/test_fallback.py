"""Direct only after crewkit's gateway pre-check fails; once a prompt is sent, its outcome stands."""

import json
import time

from tests.process.agent.harness import World


def gateway_spawns(world):
    return [e for e in world.fake() if "spawn" in e and e["headers_sha"]]


def direct_spawns(world):
    return [e for e in world.fake() if "spawn" in e and not e["headers_sha"]]


def assert_direct(world, code):
    row = world.run_row()
    assert (row["route"], row["fallback_reason"], row["outcome"]) == ("direct_fallback", code, "ok")
    assert len(gateway_spawns(world)) == 0 and len(direct_spawns(world)) == 1
    recs = world.recs()
    assert {r["llm_route"] for r in recs} == {"direct_fallback"}
    assert not any(r["downgrade"] for r in recs)


def test_a_plain_token_goes_direct(tmp_path, gateway):
    world = World(tmp_path, gateway, token="dapi-not-a-jwt")
    assert world.run().rc == 0
    assert_direct(world, "databricks_gateway_token")


def test_b_not_relayed_goes_direct(world, gateway):
    gateway.mode = "direct"
    assert world.run().rc == 0
    assert_direct(world, "databricks_gateway_not_relayed")


def test_c_503_after_the_prompt_stays(world):
    world.cue([{"turn": "error_status", "status": 503}])
    assert world.run().rc == 0
    row = world.run_row()
    assert (row["route"], row["api_error_status"], row["outcome"]) == (
        "databricks-gateway",
        503,
        "llm_unavailable",
    )
    assert row["fallback_reason"] is None
    assert world.spawns() == 1
    assert {(r["downgrade_reason"], r["llm_route"]) for r in world.recs()} == {
        ("llm_unavailable", "databricks-gateway")
    }


def test_d_retry_schedule_bound_then_close(world):
    probe = world.root / "probe.jsonl"
    world.seams["turn_s"] = 4
    world.cue([{"turn": "retry_schedule"}])
    t0 = time.monotonic()
    assert world.run(driver="child_probe", env={"CHILD_PROBE_OUT": str(probe)}).rc == 0
    row = world.run_row()
    assert (row["route"], row["outcome"], row["api_error_status"]) == (
        "databricks-gateway",
        "llm_incomplete",
        None,
    )
    assert world.spawns() == 1
    (seen,) = [json.loads(x) for x in probe.read_text().splitlines()]
    assert seen["session_s"] < 4 + 10
    assert seen["children"] == []
    assert time.monotonic() - t0 < 60
    assert {r["downgrade_reason"] for r in world.recs()} == {"llm_incomplete"}


def test_e_initialize_hang_holds(world):
    world.cue([{"init": "hang"}])
    assert world.run(driver="fast_connect").rc == 0
    row = world.run_row()
    assert (row["route"], row["outcome"]) == ("none", "llm_unavailable")
    assert "connect_timeout" in (row["error"] or "")
    assert world.spawns() == 1


def test_f_no_time_left_for_direct(tmp_path, gateway):
    world = World(tmp_path, gateway, token="dapi-not-a-jwt")
    world.seams["clock_offset_s"] = 1100
    assert world.run().rc == 0
    row = world.run_row()
    assert (row["route"], row["error"], row["outcome"]) == ("none", "budget", "llm_unavailable")
    assert world.spawns() == 0
    assert {r["downgrade_reason"] for r in world.recs()} == {"llm_unavailable"}
