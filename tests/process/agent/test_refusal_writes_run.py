"""A bearer account on the gateway route: refused before any prompt, every date held, one run row."""


def test_bearer_refused(world):
    world.cue([{"account": "bearer"}])
    assert world.run().rc == 0
    assert world.spawns() == 1
    assert "user" not in world.frames()
    row = world.run_row()
    assert (row["outcome"], row["refusal"], row["route"]) == (
        "auth_refused",
        "token_present",
        "none",
    )
    recs = world.recs()
    assert len(recs) == 7
    assert {(r["downgrade"], r["downgrade_reason"]) for r in recs} == {(True, "llm_unavailable")}
