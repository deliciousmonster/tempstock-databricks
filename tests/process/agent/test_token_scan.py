"""The canary token reaches the CLI child and nothing the worker writes: logs, run lines, dumps, traces.

Five hours: direct, refused, a clean gateway hour, a 401 echoing the request, a deadline after the fetch.
"""

import hashlib
import json

from tests.process.agent.harness import CANARY
from weather_signal.agent.runlog import innermost_main_frame

NEEDLES = (CANARY, CANARY.rsplit(".", 1)[1], CANARY[-16:])


def scan(world):
    hits = []
    files = [
        world.log,
        *world.cache.glob("*.jsonl"),
        *world.cache.glob("deadline-*.txt"),
        *world.store.glob("*.jsonl"),
    ]
    files += [p for p in (world.root / "mlruns").rglob("*") if p.is_file()]
    for p in files:
        data = p.read_bytes()
        hits += [f"{p.name}:{n[:6]}" for n in NEEDLES if n.encode() in data]
    return hits, len(files)


def canary_header_sha():
    return hashlib.sha256(json.dumps({"Authorization": f"Bearer {CANARY}"}).encode()).hexdigest()


def test_five_hours_leak_nothing(world, gateway):
    gateway.mode = "direct"
    assert world.run().rc == 0
    assert world.run_row()["route"] == "direct_fallback"

    gateway.mode = "relayed"
    world.cue([{}, {"account": "bearer"}])
    assert world.run().rc == 0
    shas = [e["headers_sha"] for e in world.fake() if e.get("headers_sha")]
    assert shas, "the canary never reached a child, so the scan would prove nothing"

    world.cue([{}])  # a clean gateway hour: the connected path runs with the token in the child env
    assert world.run().rc == 0
    assert [x["route"] for x in world.lines() if x.get("line") != "started"][
        -1
    ] == "databricks-gateway"

    gateway.mode = "echo401"
    assert world.run().rc == 0

    gateway.mode = "relayed"
    world.seams["deadline_s"] = 10  # past MLflow's import, so the fetch has happened
    assert world.run(hourly=True, driver="after_token_block", timeout=60).rc in (
        1,
        -9,
    )  # -9: the backstop
    (dump,) = world.cache.glob("deadline-*.txt")
    assert innermost_main_frame(dump.read_text()).endswith(" in stuck")

    hits, n = scan(world)
    assert n >= 5
    assert hits == []
    assert any("databricks_gateway_auth" in json.dumps(x) for x in world.lines())
