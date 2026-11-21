"""The proof's read-only checks after bootstrap, and after the agent's next run; each writes a report."""

from __future__ import annotations

import json
import os
import plistlib
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from weather_signal.agent import launchd
from weather_signal.deploy import phases, proof_outlook, schedules
from weather_signal.deploy import proof_checks as pc
from weather_signal.deploy.cli import Unknown
from weather_signal.signal.temperature_index import NORMALS_YEARS

if TYPE_CHECKING:
    from weather_signal.deploy.proof import Proof

SETUP_TABLES = (("signals", "signal_backtest"), *(("signals", t) for t in pc.LIVE_TABLES))
DONE_STATES = ("TERMINATED", "INTERNAL_ERROR", "SKIPPED")
LOG_LINES = 200


def _bootstrap_record(p: Proof) -> tuple[dict[str, Any], str]:
    try:
        doc = json.loads((p.out / "bootstrap.json").read_text(encoding="utf-8"))
        log = (p.out / "bootstrap.log").read_text(encoding="utf-8")
    except (OSError, ValueError) as err:
        raise Unknown(f"no bootstrap record in {p.out}: {type(err).__name__}") from err
    if doc.get("rc") is None:
        raise Unknown(f"the bootstrap in {p.out} began {doc.get('began')} and recorded no exit: "
                      "it was stopped or is still running")  # fmt: skip
    return doc, log


def _counts(p: Proof, tables: set[tuple[str, str]]) -> dict[str, int | None]:
    s = p.ctx.scope
    out: dict[str, int | None] = {}
    for schema, table in sorted(tables):
        name = s.names.table(schema, table)
        try:
            rows = p.ctx.sql(f"SELECT count(*) AS n FROM {name}")
            out[name] = int(rows[0]["n"])
        except Unknown:
            out[name] = None
    return out


def _normals(p: Proof) -> list[pc.Check]:
    lo, hi = NORMALS_YEARS
    rows = p.ctx.sql(
        "SELECT period, count(DISTINCT day_of_year) AS days, "
        "count(DISTINCT CASE WHEN normal_c IS NOT NULL THEN day_of_year END) AS valued "
        f"FROM {p.ctx.scope.names.table('signals', 'normals')} GROUP BY period"
    )
    return [pc.normals_check(rows, f"{lo}-{hi}")]


def jobs_by_name(p: Proof) -> dict[str, list[dict[str, Any]]]:
    pre = p.ctx.scope.prefix
    return {
        f"{pre}{n}": list(p.ctx.cli("jobs", "list", "--name", f"{pre}{n}") or [])
        for n in schedules.jobs(p.ctx.repo)
    }


def _app(p: Proof) -> list[pc.Check]:
    doc = p.ctx.app()
    dep = str(((doc or {}).get("active_deployment") or {}).get("deployment_id") or "")
    s = p.ctx.scope
    argv = ["databricks", "apps", "logs", s.app_name, "--tail-lines", str(LOG_LINES)]
    # Read the lines even on exit 1: its stream can close with 1006 after printing them.
    r = p.ctx.run([*argv, "--profile", s.profile])
    check = pc.app_log_check(r.out, dep)
    if not check.ok and r.rc != 0:
        check = pc.Check(check.name, False, f"{check.detail}; apps logs exit {r.rc}: {r.tail(1)}")
    return [pc.app_check(doc), check]


def _agent_profile(p: Proof) -> pc.Check:
    rows = p.ctx.cli("service-principals", "list") or []
    want = [str(r["applicationId"]) for r in rows if r.get("displayName") == "weather-signal-agent"]
    argv = ["databricks", "current-user", "me", "--profile", launchd.PROFILES[0], "-o", "json"]
    r = p.ctx.run(argv)
    me = json.loads(r.out or "{}").get("userName") if r.rc == 0 else None
    ok = r.rc == 0 and bool(want) and me == want[0]
    return pc.Check("agent secret profile", ok, f"current-user me exit {r.rc}, is the agent: {ok}")


def _launch_agent(p: Proof) -> pc.Check:
    path = launchd.plist_path(p.ctx.home)
    doc = plistlib.loads(path.read_bytes()) if path.is_file() else None
    bin_dir = p.main / ".venv-connect" / "bin"
    check = pc.launch_agent_check(doc, bin_dir, p.ctx.scope.prefix, launchd.PROFILES[0])
    loaded = p.ctx.run(["launchctl", "print", f"gui/{os.getuid()}/{launchd.LABEL}"]).rc == 0
    return pc.Check(check.name, check.ok and loaded, f"{check.detail}; loaded {loaded}")


def _score_run(p: Proof, job: list[dict[str, Any]], after: datetime, wait_s: float) -> pc.Check:
    if len(job) != 1:
        return pc.score_run_check(None)
    waited, after_ms = 0.0, int(after.timestamp() * 1000)
    while True:
        runs = p.ctx.cli("jobs", "list-runs", "--job-id", str(job[0]["job_id"])) or []
        run = pc.first_scheduled(runs, after_ms)
        life = ((run or {}).get("state") or {}).get("life_cycle_state")
        if (run is not None and life in DONE_STATES) or waited >= wait_s:
            return pc.score_run_check(run)
        p.ctx.sleep(60.0)
        waited += 60.0


def verify(p: Proof, wait_s: float = 75 * 60) -> int:
    rec, log = _bootstrap_record(p)
    checks = [pc.phases_check(log, int(rec["rc"]), [x.name for x in phases.PHASES])]
    built = phases.pipeline_tables(p.ctx.repo) | set(SETUP_TABLES)
    checks += pc.table_checks(_counts(p, built))
    checks += pc.guarded("normals period", lambda: _normals(p))
    jobs = jobs_by_name(p)
    checks += pc.job_checks(
        jobs, [f"{p.ctx.scope.prefix}{n}" for n in schedules.scheduled_jobs(p.ctx.repo)]
    )
    checks += [*_app(p), _agent_profile(p), _launch_agent(p)]
    ended = datetime.fromisoformat(rec["ended"])
    score = jobs.get(f"{p.ctx.scope.prefix}weather-signal-score", [])
    checks += pc.guarded("first scheduled score run", lambda: [_score_run(p, score, ended, wait_s)])
    checks += pc.table_checks(_counts(p, {("signals", pc.SCORED)}))
    checks += proof_outlook.checks(p, datetime.fromisoformat(rec["began"]), wait_s)
    notes = [f"bootstrap ended {rec['ended']}", f"verified {p.ctx.now().isoformat()}"]
    return _write(p, "verify", "dev proof: verify", checks, notes)


def bootstrap_failed(p: Proof, rc: int, resume: str) -> int:
    """The verify report for a bootstrap that exited non-zero: its phases and the way back, no poll."""
    rec, log = _bootstrap_record(p)
    checks = [pc.phases_check(log, rc, [x.name for x in phases.PHASES])]
    notes = [
        f"bootstrap exited {rc} at {rec.get('ended')}; verify skipped, dev is not built",
        f"to finish: {resume}",
    ]
    _write(p, "verify", "dev proof: verify", checks, notes)
    p.say(f"bootstrap: exit {rc}; verify skipped. To finish: {resume}")
    return rc


def _agent_sql(p: Proof, since: datetime) -> tuple[Any, list[str], list[Any], list[Any]]:
    n = p.ctx.scope.names
    runs_t, signal_t = n.table("signals", "agent_runs"), n.table("signals", "signal")
    runs = p.ctx.sql(
        f"SELECT a.run_id, a.as_of, a.outcome, a.started_at, s.as_of IS NOT NULL AS has_signal "
        f"FROM {runs_t} a LEFT JOIN (SELECT DISTINCT as_of FROM {signal_t}) s "
        f"ON s.as_of = a.as_of WHERE a.started_at >= timestamp_seconds({int(since.timestamp())}) "
        "ORDER BY a.started_at"
    )
    run, earlier = pc.graded_run(runs)
    if run is None:
        return None, earlier, [], []
    rid = str(run["run_id"])
    if not re.fullmatch(r"[0-9a-f-]{8,64}", rid):
        raise Unknown(f"agent run id {rid!r} is not a hex id")
    run_cte = f"WITH run AS (SELECT * FROM {runs_t} WHERE run_id = '{rid}')"
    recs = p.ctx.sql(
        f"{run_cte} SELECT r.target_date, r.signal_action, r.action, r.downgrade_reason "
        f"FROM {n.table('signals', 'recommendations')} r JOIN run ON r.as_of = run.as_of "
        "AND r.written_at >= run.started_at"
    )
    signal = p.ctx.sql(
        f"{run_cte} SELECT s.target_date, s.action FROM {signal_t} s JOIN run ON s.as_of = run.as_of"
    )
    return run, earlier, recs, signal


def verify_agent(p: Proof, since: datetime | None) -> int:
    if since is None:
        since = datetime.fromisoformat(_bootstrap_record(p)[0]["ended"])
    run, earlier, recs, signal = _agent_sql(p, since)
    checks = pc.agent_checks(run, recs, signal)
    graded = f"graded run {run['run_id']} as_of {run['as_of']}" if run else "no graded run yet"
    notes = [f"agent runs started at or after {since.isoformat()}; {graded}", *earlier]
    return _write(p, "verify-agent", "dev proof: agent run", checks, notes)


def _write(p: Proof, stem: str, title: str, checks: list[pc.Check], notes: list[str]) -> int:
    md, doc = pc.report(title, checks, notes)
    p.write(f"{stem}.md", md)
    p.write(f"{stem}.json", doc)
    verdict = md.splitlines()[0].rsplit(" ", 1)[-1]
    p.say(f"{stem}: {verdict}, report {p.out / (stem + '.md')}")
    return 0 if verdict == "PASS" else 1
