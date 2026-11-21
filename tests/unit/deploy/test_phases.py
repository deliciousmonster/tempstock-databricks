"""The driver: phase order, a done workspace written to by nothing, a silent probe stopping all writes."""

import pytest

from weather_signal.deploy import phases
from weather_signal.deploy.cli import Result

ORDER = [
    "preflight",
    "principals",
    "bundle",
    "secret",
    "seed",
    "catchup",
    "pipeline",
    "setup",
    "genie",
    "app",
    "train",
    "refresh",
    "agent",
    "live",
    "gitfolder",
]


def only(*names):
    return tuple(p for p in phases.PHASES if p.name in names)


def test_the_phases_run_in_the_spec_order():
    assert [p.name for p in phases.PHASES] == ORDER


@pytest.fixture
def empty_archive(tmp_path):
    (tmp_path / "archive" / "raw").mkdir(parents=True)
    (tmp_path / "archive" / "manifest.json").write_text('{"files": []}', encoding="utf-8")
    return tmp_path / "archive"


def _not_catch_up(ws):
    """Writes other than the catch-up's ingest run, which lands only raw files absent by key."""
    return [a for a in ws.writes() if a[1:4] != ["bundle", "run", "ingest"]]


def test_a_done_workspace_gets_no_write_but_the_catch_up_and_setups_regrant(ctx, ws, empty_archive):
    ctx.seed_from = empty_archive
    assert phases.drive(ctx) == 0
    assert len(ws.writes()) == 1 and _not_catch_up(ws) == []
    assert ctx.store.puts == []
    assert len(ctx.setups) == 1 and ctx.setups[0][1:] == ("r-1", "a-1", "app-1")
    assert ctx.lines[-2:] == ["live: done", "gitfolder: done"]


@pytest.mark.parametrize(
    "verb",
    ["service-principals list", "bundle plan", "secrets list-secrets", "apps get", "tables list"],
)
def test_a_probe_that_does_not_answer_stops_with_exit_1_before_any_write(
    ctx, ws, verb, empty_archive
):
    ctx.seed_from = empty_archive
    ws.broken.add(verb)
    assert phases.drive(ctx) == 1
    assert _not_catch_up(ws) == []
    assert "a probe did not answer" in ctx.lines[-1]


def test_a_missing_principal_is_created_with_both_entitlements(ctx, ws):
    ws.principals = ws.principals[:1]
    created = []

    def create(argv, stdin=None):
        if argv[1:3] == ["service-principals", "create"]:
            created.append(argv)
            ws.principals.append({"displayName": "weather-signal-agent", "applicationId": "a-2"})
        return Result(0, "{}", "")

    ws.rc = {}
    real = ws._answer
    ws._answer = lambda verb, a: create(a) if verb == "service-principals create" else real(verb, a)
    ctx.principals = {}
    assert phases.principals_done(ctx) is phases.State.NOT_DONE
    phases.principals_run(ctx)
    assert '"workspace-access"' in created[0][4] and '"databricks-sql-access"' in created[0][4]
    assert "weather-signal-runner" not in created[0][4]
    assert phases.principals_done(ctx) is phases.State.DONE and ctx.principals["agent"] == "a-2"


def test_two_principals_of_one_name_refuse_with_exit_2(ctx, ws):
    ws.principals.append({"displayName": "weather-signal-runner", "applicationId": "r-2"})
    assert phases.drive(ctx, only("principals")) == 2
    assert ws.writes() == []


def test_a_bundle_change_deploys_without_auto_approve_and_rechecks(ctx, ws):
    ws.plan = {"plan": {"resources.jobs.train": {"action": "create"}}}
    deployed = []
    real = ws._answer

    def answer(verb, a):
        if verb == "bundle deploy":
            deployed.append(a)
            ws.plan = {"plan": {"resources.jobs.train": {"action": "skip"}}}
        return real(verb, a)

    ws._answer = answer
    ctx.warehouse_id = "wh-1"
    assert phases.drive(ctx, only("bundle")) == 0
    assert "--auto-approve" not in deployed[0] and "warehouse_id=wh-1" in deployed[0]
    assert ws.stdin[ws.calls.index(deployed[0])] is None  # the runner closes stdin itself


def test_a_bundle_still_changing_after_deploy_is_exit_1(ctx, ws):
    ws.plan = {"plan": {"resources.jobs.train": {"action": "create"}}}
    assert phases.drive(ctx, only("bundle")) == 1
    assert "still not done" in ctx.lines[-1]


def test_a_denied_plan_entry_deploys_nothing(ctx, ws):
    ws.plan = {"plan": {"resources.volumes.raw": {"action": "recreate"}}}
    assert phases.drive(ctx, only("bundle")) == 1
    assert ws.writes() == []


def test_the_key_goes_over_stdin_never_argv(ctx, ws):
    ws.secrets = []
    phases.secret_run(ctx)
    put = ws.calls[-1]
    assert put[1:3] == ["secrets", "put-secret"] and put[3] == "weather-signal-dev"
    assert not any("k-123" in a for a in put) and ws.stdin[-1] == "k-123"


def test_a_scratch_scope_names_its_own_secret_scope(ctx):
    from weather_signal.deploy import scope

    ctx.scope = scope.build("p", "scratch", "t1_", "workspace")
    assert phases.secret_scope(ctx) == "weather-signal-t1_"


def test_the_app_hitting_the_free_daily_limit_is_exit_75(ctx, ws):
    ws.app["active_deployment"]["status"]["state"] = "FAILED"
    ws.rc["bundle run"] = Result(1, "", "Error: apps have reached the free daily limit")
    assert phases.drive(ctx, only("app")) == 75


def test_skip_app_never_asks_the_app(ctx, ws):
    ctx.skip_app = True
    ws.broken.add("apps get")
    assert phases.drive(ctx, only("app")) == 0


def test_prod_checks_its_app_like_every_target(ctx, ws):
    """prod sets run_as per job, so it carries the App and the phase may not skip it."""
    from weather_signal.deploy import scope

    ctx.scope = scope.build("p", "prod", None, "workspace")
    ws.broken.add("apps get")
    assert phases.drive(ctx, only("app")) == 1
    assert not any("none on prod" in line for line in ctx.lines)


def test_a_champion_of_another_fit_is_retrained(ctx, ws):
    ctx.champion_tags = lambda s: {"fit_as_of": "2020-01-02T21:30:00Z", "fit": "threshold_wf v1"}
    assert phases.drive(ctx, only("train")) == 1  # the fake's run changes nothing
    assert ["bundle", "run", "train"] == ws.writes()[0][1:4]


def test_an_unknown_snapshot_presence_stops(ctx, ws):
    ctx.store.unknown_heads.add("/Volumes/workspace/dev_signals/ui/snapshot.json")
    assert phases.drive(ctx, only("refresh")) == 1
    assert ws.writes() == [] and "a probe did not answer" in ctx.lines[-1]


def test_the_seed_line_names_skipped_tickers_and_the_empty_ledger(ctx, tmp_path):
    from collections import Counter

    from weather_signal.deploy.seed import Entry

    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "a.gz").write_bytes(b"x")
    ctx.seed_from, ctx.seed = tmp_path, [Entry("a.gz", 1, "")]
    ctx.seed_skipped = Counter({"AAA": 3, "BBB": 2})
    phases.seed_run(ctx)
    assert ctx.lines[-1] == (
        "seed: uploaded 1 of 1 archive files; 5 price envelopes for tickers outside config "
        "skipped: AAA, BBB; the ledger volume starts empty (the archive holds raw files only)"
    )


def test_a_sql_error_is_unknown_not_a_traceback(monkeypatch):
    from weather_signal import spark
    from weather_signal.deploy import command
    from weather_signal.deploy.cli import Unknown

    class Broken:
        def sql(self, q):
            raise RuntimeError("[TABLE_OR_VIEW_NOT_FOUND]")

    monkeypatch.setattr(spark, "spark_session", lambda profile: Broken())
    with pytest.raises(Unknown, match="SQL did not answer"):
        command._connect_sql("p")("SELECT 1")


def test_the_catch_up_runs_the_ingest_job_after_the_seed_and_before_the_pipeline(ctx, ws):
    names = [p.name for p in phases.PHASES]
    assert names.index("seed") < names.index("catchup") < names.index("pipeline")
    assert phases.drive(ctx, only("catchup")) == 0
    run = [a for a in ws.writes() if a[1:4] == ["bundle", "run", "ingest"]]
    assert run and run[0][-2:] == ["--params", "catch_up=true"]
