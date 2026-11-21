"""The proof after a destroy: resume with the first run's ledger, drain the agent, print the way back."""

import pytest

from tests.unit.deploy.conftest import ok
from tests.unit.deploy.proof_world import _destroys
from weather_signal.deploy import phases, preflight, proof, scope
from weather_signal.deploy.cli import Unknown


def _residue_once(ws):
    """The App's delete outlasts the residue wait once: the first run stops after the destroy."""
    real = ws._answer
    ws.stuck = True
    ws._answer = lambda v, a: (
        ok([{"name": "dev-weather-signal-ui"}]) if v == "apps list" and ws.stuck else real(v, a)
    )


def _ledger_arg(argv):
    return argv[argv.index("--ledger-from") + 1]


# A stop between the destroy and the bootstrap resumes with the first run's ledger.


def test_a_new_out_after_the_destroy_is_refused_and_points_at_resume(p, ws, tmp_path):
    _residue_once(ws)
    with pytest.raises(proof.Stop, match="residue"):
        proof.run(p)
    assert (p.out / "archive/ledger/alphavantage.jsonl").read_bytes() == b"1234"
    ws.stuck = False
    p.out = tmp_path / "second"
    with pytest.raises(proof.Stop, match="--resume") as err:
        proof.run(p)
    assert err.value.code == 2 and ws.streamed == []


def test_resume_bootstraps_with_the_first_runs_ledger_and_archives_nothing_again(p, ws):
    _residue_once(ws)
    with pytest.raises(proof.Stop, match="residue"):
        proof.run(p)
    ws.stuck = False
    pauses = [c for c in ws.calls if c[1:3] == ["jobs", "update"]]
    assert proof.main(
        ["run", "--yes", "--resume", "--profile", "p", "--seed-from", "x", "--out", str(p.out)],
        make=lambda a: p,
    ) == 0  # fmt: skip
    (argv,) = ws.streamed
    assert _ledger_arg(argv) == str(p.out / "archive/ledger")
    assert (p.out / "archive/ledger/alphavantage.jsonl").read_bytes() == b"1234"
    assert [c for c in ws.calls if c[1:3] == ["jobs", "update"]] == pauses
    assert len(_destroys(ws)) == 1


def test_resume_without_a_complete_archive_is_refused(p, ws):
    with pytest.raises(proof.Stop, match="no complete archive"):
        proof.run(p, resume=True)
    assert ws.writes() == []


def test_resume_while_dev_is_still_deployed_and_no_destroy_began_is_refused(p, ws):
    p.write("archive.txt", "raw: 1 files\n")
    with pytest.raises(proof.Stop, match="holds no run.json"):
        proof.run(p, resume=True)
    assert ws.writes() == []


# The agent is unloaded before the export; the recount is the last read before the destroy.


def test_the_agent_is_drained_and_unloaded_before_the_export(p, ws):
    order = []
    run, sql = p.ctx.run, p.ctx.sql
    ws.agent_running = True

    def sleep(s):
        ws.agent_running = False

    def spy(argv, stdin=None):
        if argv[:2] == ["launchctl", "bootout"]:
            order.append("bootout")
        return run(argv, stdin)

    def spy_sql(q):
        if q.startswith("SELECT * FROM"):
            order.append("export")
        return sql(q)

    p.ctx.run, p.ctx.sql, p.ctx.sleep = spy, spy_sql, sleep
    assert proof.run(p) == 0
    assert order[0] == "bootout" and "export" in order


def test_a_row_written_after_the_first_recount_still_stops_the_destroy(p, ws):
    real = ws._answer

    def answer(verb, a):
        if verb == "warehouses list":  # read after the first recount, before the destroy
            ws.rows["workspace.dev_signals.agent_runs"] = [{"run_id": "late"}]
        return real(verb, a)

    ws._answer = answer
    with pytest.raises(proof.Stop, match="agent_runs holds 1 rows, 0 archived"):
        proof.run(p)
    assert _destroys(ws) == []


# Every stop after the pause prints the way back, with --profile.


def test_a_probe_that_does_not_answer_after_the_pause_prints_the_way_back(p, ws):
    ws.broken.add("volumes read")
    with pytest.raises(Unknown):
        proof.run(p)
    line = p.ctx.lines[-1]
    assert "To finish:" in line and "bootstrap --profile p --target dev" in line


def test_ctrl_c_in_the_drain_prints_the_way_back(p, ws):
    ws.active = [{"run_id": 9}]

    def interrupt(s):
        raise KeyboardInterrupt

    p.ctx.sleep = interrupt
    with pytest.raises(KeyboardInterrupt):
        proof.run(p)
    assert "bootstrap --profile p --target dev" in p.ctx.lines[-1]


def test_a_stop_after_the_destroy_names_the_resume_command(p, ws):
    _residue_once(ws)
    with pytest.raises(proof.Stop):
        proof.run(p)
    line = p.ctx.lines[-1]
    assert f"run --yes --resume --profile p --seed-from {p.seed_from} --out {p.out}" in line


def test_the_live_phase_unpauses_schedules_paused_by_hand_that_the_plan_does_not_show(ctx, ws):
    ws.jobs = dict.fromkeys(
        [
            "dev_weather-signal-ingest",
            "dev_weather-signal-score",
            "dev_weather-signal-train",
            "dev_weather-signal-ui-snapshot",
        ],
        "PAUSED",
    )  # the plan's fake shows no change: the default answer is {"action": "skip"}
    assert phases.drive(ctx, tuple(x for x in phases.PHASES if x.name == "live")) == 0
    assert set(ws.jobs.values()) == {"UNPAUSED"}
    assert [c for c in ws.calls if c[1:3] == ["bundle", "deploy"]] == []


# --ledger-from is dev's alone.


@pytest.mark.parametrize(("target", "prefix"), [("prod", None), ("scratch", "t9_")])
def test_ledger_from_is_refused_off_dev(ctx, ws, tmp_path, target, prefix):
    ctx.scope, ctx.ledger_from = scope.build("p", target, prefix, "workspace"), tmp_path
    with pytest.raises(preflight.Stop, match="ledger-from"):
        preflight.check(ctx)
    assert ws.writes() == [] and ws.calls == []
