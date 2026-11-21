"""The proof's judgements, pure: the dev-only destroy guard, the archive diff and each pass/fail check."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from weather_signal.deploy.scope import Scope

# The field naming each deployed resource, and what that name must start with on dev.
OWN = {
    "schemas": "name",
    "jobs": "name",
    "pipelines": "name",
    "apps": "name",
    "dashboards": "display_name",
    "volumes": "schema_name",
    "registered_models": "schema_name",
    "secret_scopes": "scope",
}
CHILDREN = ("permissions", "grants")
DEV_SECRET_SCOPE = "weather-signal-dev"
# Pipeline tables a fresh land may leave empty; every other table must hold rows.
# prices_intraday fills only in session hours, so a proof run overnight finds it empty.
MAY_BE_EMPTY = (
    "forecast_fill_quarantine",
    "forecast_quarantined_days",
    "forecast_fill_check",
    "prices_intraday",
)
LIVE_TABLES = ("recommendations", "agent_runs")  # filled by the agent; verify-agent judges them
SCORED = "signal"  # filled by the scheduled score run, so counted after verify waits for it


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def refuse_unless_dev(s: Scope) -> None:
    if s.target != "dev" or s.prefix != "dev_" or s.app_prefix != "dev-":
        raise ValueError(f"the proof destroys only dev_ (got {s.target}, {s.prefix!r})")


def _wants(typ: str, s: Scope) -> tuple[str, bool]:
    """(the required value, whether it is exact) for one resource type."""
    if typ == "secret_scopes":
        return DEV_SECRET_SCOPE, True
    return (s.app_prefix if typ == "apps" else s.prefix), False


def destroy_refusals(doc: Any, s: Scope) -> list[str]:
    """Each deployed resource `bundle destroy` would delete that is not a dev_ object; [] to go on."""
    refuse_unless_dev(s)
    state = doc.get("state") if isinstance(doc, Mapping) else None
    if not isinstance(state, Mapping):
        return ["the deployed state has no `state` object"]
    out = []
    for key, v in state.items():
        parts = str(key).split(".")
        if len(parts) == 4 and parts[3] in CHILDREN and ".".join(parts[:3]) in state:
            continue  # a parent's grants or permissions go with the parent checked here
        if len(parts) != 3 or parts[0] != "resources" or parts[1] not in OWN:
            out.append(f"{key}: not a resource type dev deploys")
            continue
        st = (v.get("state") if isinstance(v, Mapping) else None) or {}
        value, field = str(st.get(OWN[parts[1]]) or ""), OWN[parts[1]]
        want, exact = _wants(parts[1], s)
        if not (value == want if exact else value.startswith(want)):
            out.append(f"{key}: {field} {value!r} is not {'' if exact else 'under '}{want!r}")
        if st.get("catalog_name") not in (None, s.catalog):
            out.append(f"{key}: catalog {st.get('catalog_name')!r} is not {s.catalog!r}")
    return out


def volume_only(have: Mapping[str, bytes], local: Path | None) -> list[str]:
    """Volume files the local folder lacks or holds with other bytes: what a destroy would lose.
    With no local folder every file counts."""
    out = []
    for path, data in sorted(have.items()):
        p = None if local is None else local / path
        if p is None or not p.is_file() or _digest(p.read_bytes()) != _digest(data):
            out.append(path)
    return out


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def phases_check(log: str, rc: int, names: Sequence[str]) -> Check:
    """Exit 0 and each phase's line `<name>: done` or `<name>: ran`, none stopped."""
    lines = log.splitlines()
    missing = [n for n in names if not any(x in (f"{n}: done", f"{n}: ran") for x in lines)]
    stopped = [x for x in lines if ": stopped" in x]
    ok = rc == 0 and not missing and not stopped
    why = f"exit {rc}" + (f"; no line for {', '.join(missing)}" if missing else "")
    return Check("bootstrap phases", ok, why + (f"; {stopped[0]}" if stopped else ""))


def table_checks(counts: Mapping[str, int | None]) -> list[Check]:
    out = []
    for table, n in sorted(counts.items()):
        short = table.rsplit(".", 1)[-1]
        may_be_empty = short in MAY_BE_EMPTY or short in LIVE_TABLES
        ok = n is not None and (n > 0 or may_be_empty)
        out.append(
            Check(f"table {table}", ok, "absent or unreadable" if n is None else f"{n} rows")
        )
    return out


def normals_check(rows: Sequence[Mapping[str, Any]], want: str) -> Check:
    """`signals.normals` reads one period, the standard one, with a normal for all 366 days. A
    fallback to the training half names its own period; a short day keeps its row with a null."""
    seen = ", ".join(
        f"{r['period']} {r['valued']} of {r['days']} days with a normal"
        for r in sorted(rows, key=lambda r: str(r["period"]))
    )
    ok = len(rows) == 1 and str(rows[0]["period"]) == want and int(rows[0]["valued"]) == 366
    return Check("normals period", ok, f"{seen or 'no rows'}; want {want}, 366 days with a normal")


def failed(name: str, err: Exception) -> Check:
    return Check(name, False, f"did not answer: {type(err).__name__}: {str(err)[:200]}")


def guarded(name: str, make: Callable[[], list[Check]]) -> list[Check]:
    """The checks `make` returns, or one failed check named `name` when it raises."""
    try:
        return make()
    except Exception as err:  # noqa: BLE001 - a failed check, so the report is still written
        return [failed(name, err)]


def job_checks(found: Mapping[str, list[dict[str, Any]]], scheduled: Iterable[str]) -> list[Check]:
    """One job per name; each scheduled one UNPAUSED."""
    sched, out = set(scheduled), []
    for name, rows in sorted(found.items()):
        if len(rows) != 1:
            out.append(Check(f"job {name}", False, f"{len(rows)} jobs by this name"))
            continue
        status = ((rows[0].get("settings") or {}).get("schedule") or {}).get("pause_status")
        ok = name not in sched or status == "UNPAUSED"
        out.append(Check(f"job {name}", ok, f"schedule {status or 'none'}"))
    return out


def app_check(doc: Mapping[str, Any] | None) -> Check:
    if doc is None:
        return Check("app running", False, "absent")
    app = (doc.get("app_status") or {}).get("state")
    compute = (doc.get("compute_status") or {}).get("state")
    deploy = ((doc.get("active_deployment") or {}).get("status") or {}).get("state")
    ok = (app, compute, deploy) == ("RUNNING", "ACTIVE", "SUCCEEDED")
    return Check("app running", ok, f"app {app}, compute {compute}, deployment {deploy}")


# Expected shapes, not seen on dev: an exit with a nonzero code, or a traceback no request raised.
_EXIT = re.compile(r"\b(?:exited with|exit) (?:code|status)\b[:= ]*(-?\d+)", re.IGNORECASE)
_REQUEST_ERROR = "Exception in ASGI application"  # Uvicorn's line before a handled 500's traceback
_CHAINED = ("During handling of the above exception", "The above exception was the direct cause")


def _crashes(lines: list[str]) -> list[str]:
    out, handled = [], False  # handled: the traceback being read follows a request's error line
    for i, x in enumerate(lines):
        m = _EXIT.search(x)
        if m is not None and int(m.group(1)) != 0:
            out.append(x)
        elif "Traceback (most recent call last)" in x:
            handled = (i > 0 and _REQUEST_ERROR in lines[i - 1]) or (handled and _chain(lines, i))
            out += [] if handled else [x]
    return out


def _chain(lines: list[str], i: int) -> bool:
    """The last line with text before line `i` says its traceback continues the one above."""
    text = (lines[j].split("[APP]", 1)[-1].strip() for j in range(i - 1, -1, -1))
    return next((t for t in text if t), "").startswith(_CHAINED)


def app_log_check(log: str, deployment_id: str) -> Check:
    """The app's own log: the active deployment started, Uvicorn finished its startup, and no
    shutdown, unhandled traceback or error exit follows the last startup. The Apps front door
    answers /healthz itself, so an HTTP probe never reaches the app."""
    lines = log.splitlines()
    starts = [i for i, x in enumerate(lines) if f"Starting deployment {deployment_id}" in x]
    if not deployment_id or not starts:
        why = f"deployment {deployment_id or '(none)'} not in the app log's last lines"
        return Check("app serving", False, why)
    after = lines[starts[-1] :]
    up = [i for i, x in enumerate(after) if "[APP]" in x and "Application startup complete" in x]
    down = [x for x in after if "[APP]" in x and "Application shutdown" in x]
    crash = _crashes(after[up[-1] :]) if up else []  # a restart that came up again is serving
    ok = bool(up) and not down and not crash
    last = [after[up[-1]]] if up else ["no 'Application startup complete'"]
    seen = (crash or down or last)[-1].split(" [")[0]
    state = (
        "crashed" if crash else "shut down" if down else "startup complete" if up else "not started"
    )
    return Check("app serving", ok, f"app log: deployment {deployment_id} {state} ({seen})")


def launch_agent_check(
    doc: Mapping[str, Any] | None, bin_dir: Path, prefix: str, profile: str
) -> Check:
    """The installed plist runs a Python in the main checkout's .venv-connect, as the agent's
    profile, against this prefix."""
    if doc is None:
        return Check("launch agent", False, "no plist")
    env = doc.get("EnvironmentVariables") or {}
    have = str((doc.get("ProgramArguments") or [""])[0])
    pre, prof = env.get("WEATHER_SIGNAL_SCHEMA_PREFIX"), env.get("WEATHER_SIGNAL_PROFILE")
    ok = Path(have).parent == bin_dir and pre == prefix and prof == profile
    return Check(
        "launch agent",
        ok,
        f"python {have}, prefix {pre!r}, profile {prof!r}; want {bin_dir}/, {prefix!r}, {profile!r}",
    )


def first_scheduled(runs: Iterable[Mapping[str, Any]], after_ms: int) -> Mapping[str, Any] | None:
    periodic = [
        r
        for r in runs
        if r.get("trigger") == "PERIODIC" and int(r.get("start_time") or 0) >= after_ms
    ]
    return min(periodic, key=lambda r: int(r["start_time"]), default=None)


def score_run_check(run: Mapping[str, Any] | None) -> Check:
    if run is None:
        return Check("first scheduled score run", False, "none started yet")
    st = run.get("state") or {}
    life, result = st.get("life_cycle_state"), st.get("result_state")
    return Check(
        "first scheduled score run",
        result == "SUCCESS",
        f"run {run.get('run_id')}: {life} {result}",
    )


def graded_run(
    runs: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any] | None, list[str]]:
    """The first run, by start, whose as_of hour has signal rows; earlier runs as info notes."""
    earlier: list[str] = []
    for r in runs:
        if r.get("has_signal"):
            return r, earlier
        earlier.append(f"info: {r.get('run_id')} as_of {r.get('as_of')}: {r.get('outcome')}, "
                       "no signal rows for its hour")  # fmt: skip
    return None, earlier


def agent_checks(
    run: Mapping[str, Any] | None,
    recs: Sequence[Mapping[str, Any]],
    signal: Sequence[Mapping[str, Any]],
) -> list[Check]:
    """The first agent run whose hour has signal rows: outcome ok, one recommendation per signal
    row, each carrying the model's action or a hold with its downgrade reason."""
    if run is None:
        why = "no graded run yet: no agent run since the bootstrap has signal rows for its hour"
        return [Check("agent run", False, why)]
    out = [
        Check("agent run", run.get("outcome") == "ok", f"{run.get('run_id')}: {run.get('outcome')}")
    ]
    by_date = {str(r["target_date"]): r for r in recs}
    unmatched = [str(s["target_date"]) for s in signal if str(s["target_date"]) not in by_date]
    out.append(
        Check(
            "a recommendation per signal row",
            bool(signal) and not unmatched,
            f"{len(recs)} recommendations, {len(signal)} signal rows"
            + (f"; none for {', '.join(unmatched)}" if unmatched else ""),
        )
    )
    model = {str(s["target_date"]): s.get("action") for s in signal}
    wrong = [d for d, r in by_date.items() if r.get("signal_action") != model.get(d)]
    out.append(Check("recommendations carry the model signal", not wrong, _dates(wrong)))
    unexplained = [
        d
        for d, r in by_date.items()
        if r.get("action") != r.get("signal_action")
        and not (r.get("action") == "hold" and str(r.get("downgrade_reason") or "").strip())
    ]
    downgrades = sum(r.get("action") != r.get("signal_action") for r in recs)
    out.append(
        Check(
            "downgrades explained",
            not unexplained,
            f"{downgrades} downgrades"
            + (f"; unexplained {_dates(unexplained)}" if unexplained else ""),
        )
    )
    return out


def _dates(ds: Sequence[str]) -> str:
    return ", ".join(sorted(ds)) if ds else "all match"


def report(title: str, checks: Sequence[Check], notes: Sequence[str] = ()) -> tuple[str, str]:
    """(markdown, json) with PASS only when every check passed."""
    verdict = "PASS" if checks and all(c.ok for c in checks) else "FAIL"
    rows = [f"| {'pass' if c.ok else 'FAIL'} | {c.name} | {c.detail} |" for c in checks]
    md = "\n".join(
        [f"# {title}: {verdict}", "", *[f"- {n}" for n in notes], "",
         "| result | check | detail |", "|---|---|---|", *rows, ""]
    )  # fmt: skip
    doc = {"verdict": verdict, "notes": list(notes), "checks": [asdict(c) for c in checks]}
    return md, json.dumps(doc, indent=1, default=str) + "\n"
