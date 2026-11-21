"""The dev proof's fake world: a deployed dev state, its volumes and tables, and a Proof wired to it."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.unit.deploy.conftest import ok
from weather_signal.agent import launchd
from weather_signal.deploy import phases, proof, scope
from weather_signal.deploy.cli import Result

RAW = "/Volumes/workspace/dev_raw/raw"
LEDGER = "/Volumes/workspace/dev_raw/ledger"
ENDED = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
# What the proof exports; named here so this file also loads on a base without the export.
SIGNALS_TABLES = (
    "signal",
    "recommendations",
    "agent_runs",
    "recommendations_eval",
    "signal_backtest",
)


def _state(**extra):
    st = {
        "resources.schemas.raw": {"state": {"name": "dev_raw", "catalog_name": "workspace"}},
        "resources.schemas.raw.grants": {"state": {}},
        "resources.jobs.score": {"state": {"name": "dev_weather-signal-score"}},
        "resources.apps.ui_app": {"state": {"name": "dev-weather-signal-ui"}},
        "resources.dashboards.ui_dashboard": {"state": {"display_name": "dev_weather-signal"}},
        "resources.volumes.raw_files": {
            "state": {"name": "raw", "catalog_name": "workspace", "schema_name": "dev_raw"}
        },
        "resources.secret_scopes.alphavantage": {"state": {"scope": "weather-signal-dev"}},
        **extra,
    }
    return {"state_version": 1, "state": st}


# `databricks apps logs` as measured on dev: an older deployment, then the active one starting.
APP_LOG = """\
2026-11-19T20:10:00Z [BUILD] [INFO] Starting deployment dep-1...
2026-11-19T20:10:09Z [APP] INFO:     Application startup complete.
2026-11-19T21:23:52Z [BUILD] [INFO] Starting deployment dep-2...
2026-11-19T21:24:00Z [APP] INFO:     Application startup complete.
2026-11-19T21:24:00Z [APP] INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
2026-11-19T21:24:01Z [BUILD] [INFO] Deployment successful
2026-11-19T22:48:04Z [HTTP] 192.0.2.1 - - "GET / HTTP/1.1" 302 489 "-" "Mozilla/5.0"
"""

UNSCHEDULED = ("dev_weather-signal-backtest",)
DEV = scope.build("p", "dev", None, "workspace")


def _volumes(ctx):
    """One raw file the data folder has, one it lacks, and a ledger file; (listing, bytes)."""
    raw = ctx.seed_from / "raw" / "yahoo/2026/11/18/21"
    raw.mkdir(parents=True)
    (raw / "local.json.gz").write_bytes(b"12345")
    vols = {
        RAW: {"yahoo/2026/11/18/21/local.json.gz": 5, "yahoo/2026/11/19/05/only.json.gz": 3},
        LEDGER: {"alphavantage.jsonl": 4},
    }
    files = {
        f"{RAW}/yahoo/2026/11/18/21/local.json.gz": b"12345",
        f"{RAW}/yahoo/2026/11/19/05/only.json.gz": b"abc",
        f"{LEDGER}/alphavantage.jsonl": b"1234",
    }
    return vols, files


def _wire(ctx, ws):
    """The deployed state, destroy and trash effects, run listings and the unscheduled jobs."""
    real = ws._answer

    def answer(verb, a):
        if verb == "workspace export" and a[3].endswith("/dev/state/resources.json"):
            return ok(ws.state) if ws.state else Result(1, "", "Error: Path doesn't exist.\n")
        if verb == "bundle destroy":
            ws.schemas, ws.app, ws.state = [], None, None
        if verb == "genie trash-space":
            ws.spaces = [s for s in ws.spaces if s["space_id"] != a[3]]
        if verb == "lakeview list":
            return ok(ws.dashboards)
        if verb == "jobs list-runs":
            return ok(ws.active if "--active-only" in a else ws.runs)
        return real(verb, a)

    def run(argv, stdin=None):
        a = list(argv)
        if a[1:3] == ["jobs", "list"] and "--name" in a and a[a.index("--name") + 1] in UNSCHEDULED:
            ws.calls.append(a)
            return ok([{"job_id": 2, "settings": {"name": a[a.index("--name") + 1]}}])
        return ws(a, stdin)

    ws._answer, ctx.run = answer, run
    ctx.sql = lambda q: _sql(ws, q)


def _sql(ws, q):
    """Rows of a signals table the proof exports; a count of any table."""
    table = q.split(" FROM ")[1].split()[0]
    rows = ws.rows.get(table)
    if q.startswith("SELECT count(*)"):
        return [{"n": 3369 if rows is None else len(rows)}]
    return list(rows or [])


@pytest.fixture
def p(ctx, ws, tmp_path):
    main = tmp_path / "main"
    (main / ".venv-connect/bin").mkdir(parents=True)
    vols, files = _volumes(ctx)
    ctx.list_volume = lambda root: vols.get(root, {})
    ws.state, ws.runs, ws.streamed, ws.active = _state(), [], [], []
    ws.rows = {f"workspace.dev_signals.{x}": [] for x in SIGNALS_TABLES}
    ws.rows["workspace.dev_signals.signal"] = [{"target_date": "2026-11-20", "action": "buy"}]
    ws.rows["workspace.dev_signals.signal_backtest"] = [{"as_of": "2026-11-18", "hit": True}]
    _outlook(ws)
    ws.jobs = dict.fromkeys(
        [
            "dev_weather-signal-ingest",
            "dev_weather-signal-score",
            "dev_weather-signal-train",
            "dev_weather-signal-ui-snapshot",
        ],
        "UNPAUSED",
    )
    _wire(ctx, ws)
    ws.app = {**ws.app, "url": "https://dev-app.example", "app_status": {"state": "RUNNING"},
              "compute_status": {"state": "ACTIVE"},
              "active_deployment": {"deployment_id": "dep-2", "status": {"state": "SUCCEEDED"}}}  # fmt: skip
    ws.rc["apps logs"] = Result(0, APP_LOG, "")
    ws.built = ws.app
    pr = proof.Proof(
        ctx=ctx, out=tmp_path / "out", seed_from=ctx.seed_from, main=main,
        python=str(main / ".venv-connect/bin/python"), read=files.get,
        stream=lambda argv, log, fd=None: _stream(ws, argv, log),
        get=lambda url: ws.http.get(url, (404, b"")),
    )  # fmt: skip
    _plist(ctx, pr.python)
    return pr


AS_OF = ENDED + timedelta(minutes=30)  # the first scheduled score after the bootstrap
BEGAN = ENDED - timedelta(hours=1)
# P 30 at 12:30 UTC: decision day 2026-11-18, horizons 0 to 6; 6 holds as horizon_unavailable.
TARGETS = [(f"2026-11-{18 + k}", k) for k in range(7)]


def _outlook(ws):
    """A held outlook at AS_OF, the forecast behind it, and the App serving it."""
    rows = [
        {"as_of_s": int(AS_OF.timestamp()), "target_date": t, "signal_action": "hold",
         "reasons": ["horizon_unavailable" if k == 6 else "no_champion"]}
        for t, k in TARGETS
    ]  # fmt: skip
    ws.rows["workspace.dev_app.v_ui_outlook"] = rows
    ws.rows["workspace.dev_raw.raw_envelopes"] = [{"s": int(ENDED.timestamp())}]
    ws.rows["workspace.dev_signals.forecast_index"] = [
        {"target_date": t, "lead_days": k + 2} for t, k in TARGETS[:6]
    ]
    ws.rows["workspace.dev_signals.normals"] = [{"period": "1961-1990", "days": 366, "valued": 366}]
    ws.dashboards = [{"dashboard_id": "d-1", "display_name": "dev_weather-signal"}]
    ws.http = {"https://dev-app.example/api/outlook": (200, api_body(rows))}


def api_body(rows):
    data = [
        {"as_of": AS_OF.strftime("%Y-%m-%dT%H:%M:%SZ"), "target_date": r["target_date"],
         "signal_action": r["signal_action"], "reasons": r["reasons"]}
        for r in rows
    ]  # fmt: skip
    return json.dumps({"data": data, "source": "snapshot"}).encode()


def verified(p, ws, lines=None, after=dict):
    """Bootstrap as streamed, then `verify`: (rc, checks by name)."""
    from weather_signal.deploy import proof_verify

    _stream(ws, ["x"], p.out / "bootstrap.log", lines)
    after()
    p.write(
        "bootstrap.json",
        json.dumps({"rc": 0, "began": BEGAN.isoformat(), "ended": ENDED.isoformat()}),
    )
    rc = proof_verify.verify(p, wait_s=0)
    doc = json.loads((p.out / "verify.json").read_text(encoding="utf-8"))
    return rc, {c["name"]: c for c in doc["checks"]}


def _stream(ws, argv, log, lines=None):
    ws.streamed.append(argv)
    ws.app = ws.built  # what bootstrap makes again
    ws.jobs = dict.fromkeys(ws.jobs, "UNPAUSED")  # its live phase
    ws.launched = True  # its agent phase
    log.parent.mkdir(parents=True, exist_ok=True)
    body = lines if lines is not None else [f"{x.name}: ran" for x in phases.PHASES]
    log.write_text("\n".join(body) + "\n", encoding="utf-8")
    ws.runs = [_run(ENDED, "SUCCESS")]
    return 0


def _run(at, result, trigger="PERIODIC"):
    return {
        "run_id": 7, "trigger": trigger, "start_time": int(at.timestamp() * 1000) + 60_000,
        "state": {"life_cycle_state": "TERMINATED", "result_state": result},
    }  # fmt: skip


def _plist(ctx, python):
    path = launchd.plist_path(ctx.home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        launchd.plist(python=python, profile="weather-signal-agent", databricks_dir="/bin",
                      home=ctx.home, prefix="dev_")
    )  # fmt: skip


def _destroys(ws):
    return [c for c in ws.calls if c[1:3] == ["bundle", "destroy"]]
