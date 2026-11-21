"""The dev proof: archive, destroy dev_ only, bootstrap, verify; all against the fake workspace."""

import json

import pytest

from tests.unit.deploy.conftest import ok
from tests.unit.deploy.proof_world import (
    APP_LOG,
    DEV,
    ENDED,
    _destroys,
    _plist,
    _run,
    _state,
)
from tests.unit.deploy.proof_world import (
    verified as _verify,
)
from weather_signal.deploy import phases, proof, proof_verify, scope
from weather_signal.deploy import proof_checks as pc
from weather_signal.deploy.cli import Result, Unknown

# The guard: only dev_ objects in the deployed state.


def test_the_dev_state_as_deployed_passes_the_guard():
    assert pc.destroy_refusals(_state(), DEV) == []


@pytest.mark.parametrize(
    ("key", "state"),
    [
        ("resources.schemas.daily", {"name": "daily", "catalog_name": "workspace"}),
        ("resources.jobs.score2", {"name": "weather-signal-score"}),
        ("resources.apps.prod", {"name": "weather-signal-ui"}),
        (
            "resources.volumes.x",
            {"name": "raw", "catalog_name": "workspace", "schema_name": "t9_raw"},
        ),
        ("resources.secret_scopes.prod", {"scope": "weather-signal"}),
        ("resources.schemas.other", {"name": "dev_x", "catalog_name": "main"}),
        ("resources.clusters.c", {"name": "dev_c"}),
        ("resources.jobs.orphan.permissions", {}),
    ],
)
def test_anything_outside_dev_is_refused(key, state):
    bad = pc.destroy_refusals(_state(**{key: {"state": state}}), DEV)
    assert len(bad) == 1 and bad[0].startswith(key)


def test_only_the_dev_scope_is_accepted():
    for s in (scope.build("p", "prod", None, "workspace"), scope.build("p", "scratch", "t9_", "w")):
        with pytest.raises(ValueError, match="only dev_"):
            pc.refuse_unless_dev(s)


def test_volume_only_keeps_files_absent_locally_or_with_other_bytes(tmp_path):
    (tmp_path / "a").write_bytes(b"12")
    (tmp_path / "b").write_bytes(b"12")
    (tmp_path / "d").write_bytes(b"12")
    have = {"a": b"12", "b": b"12345", "c": b"1", "d": b"34"}
    assert pc.volume_only(have, tmp_path) == ["b", "c", "d"]
    assert pc.volume_only(have, None) == ["a", "b", "c", "d"]


# plan: read-only.


def test_plan_writes_the_plan_and_nothing_in_the_workspace(p, ws):
    assert proof.plan(p) == 0
    text = (p.out / "plan.md").read_text(encoding="utf-8")
    assert "- none" in text and "raw: 1 files to archive" in text and "ledger: 1 files" in text
    assert "resources.schemas.raw\n" in text and "genie space g-1" in text
    assert "--target dev --seed-from" in text
    assert ws.writes() == [] and not (p.out / "archive").exists()


def test_plan_names_a_non_dev_resource_and_exits_2(p, ws):
    ws.state = _state(**{"resources.schemas.daily": {"state": {"name": "daily"}}})
    assert proof.plan(p) == 2
    assert "resources.schemas.daily" in (p.out / "plan.md").read_text(encoding="utf-8")


# run: refusals first, then archive, destroy, bootstrap, verify.


def test_run_refuses_outside_the_main_checkouts_venv(p, ws):
    p.python = "/elsewhere/.venv/bin/python"
    assert proof.main(["run", "--profile", "p", "--seed-from", "x", "--out", "o", "--yes"],
                      make=lambda a: p) == 2  # fmt: skip
    assert ws.writes() == [] and ws.streamed == []


def test_run_refuses_from_a_linked_worktree(p, ws):
    p.ctx.linked_main = lambda repo: repo.parent
    with pytest.raises(proof.Stop, match="linked worktree"):
        proof.run(p)
    assert ws.writes() == []


def test_run_without_yes_makes_nothing():
    made = []
    assert proof.main(["run", "--profile", "p", "--seed-from", "x", "--out", "o"],
                      make=made.append) == 2  # fmt: skip
    assert made == []


def test_a_non_dev_resource_stops_the_run_before_the_archive_and_the_destroy(p, ws):
    ws.state = _state(**{"resources.jobs.x": {"state": {"name": "weather-signal-score"}}})
    with pytest.raises(proof.Stop, match="non-dev_"):
        proof.run(p)
    assert ws.writes() == [] and not (p.out / "archive").exists()


def test_a_full_run_archives_destroys_dev_bootstraps_and_passes(p, ws):
    rc = proof.run(p)
    report = json.loads((p.out / "verify.json").read_text(encoding="utf-8"))
    assert rc == 0, [c for c in report["checks"] if not c["ok"]]
    assert (p.out / "archive/raw/yahoo/2026/11/19/05/only.json.gz").read_bytes() == b"abc"
    assert (p.out / "archive/ledger/alphavantage.jsonl").read_bytes() == b"1234"
    assert not (p.out / "archive/raw/yahoo/2026/11/18/21/local.json.gz").exists()
    (d,) = _destroys(ws)
    assert d[d.index("-t") + 1] == "dev" and "schema_prefix=dev_" in d and "--auto-approve" in d
    assert [c[3] for c in ws.writes() if c[1:3] == ["genie", "trash-space"]] == ["g-1"]
    assert ws.streamed[0][1:] == [
        "bootstrap", "--profile", "p", "--target", "dev", "--seed-from", str(p.seed_from),
        "--ledger-from", str(p.out / "archive/ledger"),
    ]  # fmt: skip
    assert ws.streamed[0][0] == str(p.main / ".venv-connect/bin/weather-signal")


def test_a_failed_bootstrap_ends_the_run_with_its_report_and_the_way_back(p, ws, monkeypatch):
    """proof-3: bootstrap exited 1 at catchup and verify then polled tables never built."""
    polled = []
    monkeypatch.setattr(proof_verify, "verify", lambda *a, **k: polled.append(a) or 0)

    def stream(argv, log, fd=None):
        ws.streamed.append(argv)
        lines = ["preflight: done", "seed: ran", "catchup: stopped: bundle run ingest failed"]
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 1

    p.stream = stream
    assert proof.run(p) == 1
    assert polled == []
    report = json.loads((p.out / "verify.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "FAIL"
    (check,) = report["checks"]
    assert not check["ok"] and "catchup: stopped" in check["detail"]
    way = f"{p.python} -m weather_signal.deploy.proof finish --profile p --seed-from {p.seed_from}"
    way += f" --out {p.out}"
    assert f"to finish: {way}" in report["notes"]
    assert any(f"To finish: {way}" in x for x in p.ctx.lines)


def test_an_archive_size_mismatch_stops_before_the_destroy(p, ws):
    p.read = lambda path: b"x"
    with pytest.raises(proof.Stop, match="nothing destroyed"):
        proof.run(p)
    assert _destroys(ws) == [] and ws.streamed == []


def test_residue_that_never_clears_stops_before_bootstrap(p, ws):
    real = ws._answer
    ws._answer = lambda v, a: (ok([{"name": "dev-weather-signal-ui"}]) if v == "apps list"
                               else real(v, a))  # fmt: skip
    with pytest.raises(proof.Stop, match="residue"):
        proof.run(p)
    assert ws.streamed == []


# verify: each failure shows.


def test_verify_passes_a_finished_dev(p, ws):
    rc, checks = _verify(p, ws)
    assert rc == 0 and all(c["ok"] for c in checks.values())
    assert "table workspace.dev_daily.prices_daily" in checks
    assert "job dev_weather-signal-train" in checks


def test_a_stopped_phase_fails_verify(p, ws):
    lines = [f"{x.name}: ran" for x in phases.PHASES[:-1]] + ["live: stopped: boom"]
    rc, checks = _verify(p, ws, lines)
    assert rc == 1 and not checks["bootstrap phases"]["ok"]


def test_a_paused_schedule_fails_verify(p, ws):
    rc, checks = _verify(
        p, ws, after=lambda: ws.jobs.update({"dev_weather-signal-score": "PAUSED"})
    )
    assert rc == 1 and not checks["job dev_weather-signal-score"]["ok"]


def test_a_launch_agent_on_a_worktree_fails_verify(p, ws):
    _plist(p.ctx, "/repo/.worktrees/x/.venv-connect/bin/python")
    rc, checks = _verify(p, ws)
    assert rc == 1 and not checks["launch agent"]["ok"]


def test_a_failed_or_missing_score_run_fails_verify(p, ws):
    rc, checks = _verify(p, ws)
    ws.runs = [_run(ENDED, "FAILED")]
    assert proof_verify.verify(p, wait_s=0) == 1
    ws.runs = [_run(ENDED, "SUCCESS", trigger="ONE_TIME")]
    assert proof_verify.verify(p, wait_s=0) == 1


def test_the_app_log_of_the_active_deployment_passes_app_serving(p, ws):
    rc, checks = _verify(p, ws)
    detail = checks["app serving"]["detail"]
    assert checks["app serving"]["ok"] and "dep-2 startup complete (2026-11-19T21:24:00Z)" in detail
    logs = [c for c in ws.calls if c[1:3] == ["apps", "logs"]]
    assert logs and "--follow" not in logs[0] and "-f" not in logs[0]


def test_an_app_log_stream_that_closes_with_exit_1_after_its_lines_still_passes(p, ws):
    ws.rc["apps logs"] = Result(1, APP_LOG, "Error: log stream closed with code 1006\n")
    assert proof_verify._app(p)[1].ok


@pytest.mark.parametrize(
    "log",
    [
        "",
        APP_LOG.split("2026-11-19T21:23:52Z")[0],
        APP_LOG.replace("21:24:00Z [APP] INFO:     Application startup complete.", "21:24:00Z x"),
        APP_LOG + "2026-11-19T23:00:00Z [APP] INFO:     Application shutdown complete.\n",
    ],
    ids=["empty", "only-an-older-deployment", "no-startup-line", "shut-down-after"],
)
def test_an_app_log_without_the_active_deployment_up_fails_verify(p, ws, log):
    ws.rc["apps logs"] = Result(0, log, "")
    rc, checks = _verify(p, ws)
    assert rc == 1 and not checks["app serving"]["ok"]


def test_an_agent_profile_that_does_not_sign_in_fails_verify(p, ws):
    ws.rc["current-user me"] = Result(1, "", "")
    rc, checks = _verify(p, ws)
    assert not checks["agent secret profile"]["ok"]


# verify-agent: the first run after the proof, its recommendations against the model signal.

SIGNAL = [
    {"target_date": "2026-11-20", "action": "buy"},
    {"target_date": "2026-11-21", "action": "sell"},
]


def _recs(*rows):
    keys = ("target_date", "signal_action", "action", "downgrade_reason")
    return [dict(zip(keys, r, strict=True)) for r in rows]


def test_an_ok_run_with_an_explained_downgrade_passes():
    recs = _recs(("2026-11-20", "buy", "buy", None), ("2026-11-21", "sell", "hold", "stale prices"))
    checks = pc.agent_checks({"run_id": "r", "outcome": "ok"}, recs, SIGNAL)
    assert all(c.ok for c in checks), checks


@pytest.mark.parametrize(
    ("run", "recs"),
    [
        (None, []),
        (
            {"outcome": "guard_hold"},
            _recs(("2026-11-20", "buy", "buy", None), ("2026-11-21", "sell", "sell", None)),
        ),
        ({"outcome": "ok"}, _recs(("2026-11-20", "buy", "buy", None))),
        (
            {"outcome": "ok"},
            _recs(("2026-11-20", "sell", "sell", None), ("2026-11-21", "sell", "sell", None)),
        ),
        (
            {"outcome": "ok"},
            _recs(("2026-11-20", "buy", "hold", " "), ("2026-11-21", "sell", "sell", None)),
        ),
    ],
    ids=["no-run", "guard-hold", "missing-rec", "not-the-model", "unexplained-downgrade"],
)
def test_each_agent_failure_fails(run, recs):
    assert not all(c.ok for c in pc.agent_checks(run, recs, SIGNAL))


def test_verify_agent_reads_the_first_run_after_since_and_writes_its_report(p):
    seen = []

    def sql(q):
        seen.append(q)
        if "recommendations" in q:
            return _recs(("2026-11-20", "buy", "buy", None), ("2026-11-21", "sell", "sell", None))
        if "signal.signal" in q or "dev_signals.signal " in q:
            return SIGNAL
        return [{"run_id": "0a1b2c3d", "as_of": "22:30", "outcome": "ok", "has_signal": True}]

    p.ctx.sql = sql
    assert proof_verify.verify_agent(p, ENDED) == 0
    assert f"timestamp_seconds({int(ENDED.timestamp())})" in seen[0]
    assert "PASS" in (p.out / "verify-agent.md").read_text(encoding="utf-8").splitlines()[0]


MISSING = {"run_id": "6c3dc61e", "as_of": "21:30", "outcome": "signal_missing", "has_signal": False}
GRADED = {"run_id": "9f8e7d6c", "as_of": "22:30", "outcome": "ok", "has_signal": True}


def _agent_world(p, runs):
    def sql(q):
        if "recommendations" in q:
            return _recs(("2026-11-20", "buy", "buy", None), ("2026-11-21", "sell", "sell", None))
        if "WITH run" in q:
            assert "'9f8e7d6c'" in q
            return SIGNAL
        return runs

    p.ctx.sql = sql
    rc = proof_verify.verify_agent(p, ENDED)
    return rc, (p.out / "verify-agent.md").read_text(encoding="utf-8")


def test_verify_agent_grades_the_first_run_with_signal_and_notes_earlier_missing_ones(p):
    rc, md = _agent_world(p, [MISSING, GRADED])
    assert rc == 0, md
    assert "graded run 9f8e7d6c as_of 22:30" in md
    assert "- info: 6c3dc61e as_of 21:30: signal_missing" in md
    assert "| FAIL |" not in md


def test_verify_agent_fails_when_no_run_has_signal_yet(p):
    rc, md = _agent_world(p, [MISSING])
    assert rc == 1 and "| FAIL | agent run | no graded run yet" in md
    assert "- info: 6c3dc61e" in md


def test_a_graded_run_that_is_not_ok_still_fails(p):
    rc, md = _agent_world(p, [MISSING, {**GRADED, "outcome": "guard_hold"}])
    assert rc == 1 and "| FAIL | agent run | 9f8e7d6c: guard_hold |" in md


def test_a_run_id_that_is_not_hex_is_never_put_in_sql(p):
    p.ctx.sql = lambda q: [{**GRADED, "run_id": "x' OR '1'='1"}]
    with pytest.raises(Unknown, match="not a hex id"):
        proof_verify.verify_agent(p, ENDED)
