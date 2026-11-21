"""One hour's work once the deadline is armed: Connect, reads, the Claude turn, the MERGEs and the run row."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from weather_signal.agent import refused, runlog
from weather_signal.agent.budget import DIRECT_CAP_S, GATEWAY_CAP_S, Budget
from weather_signal.agent.merge import bind
from weather_signal.agent.proposal import fill
from weather_signal.agent.route import Outcome, classify, error_text, explain
from weather_signal.agent.store import (
    GUARD_OK,
    GUARD_UNVERIFIED,
    EvalStore,
    FileStore,
    SparkStore,
    Store,
)
from weather_signal.config import Names

log = logging.getLogger("weather_signal.agent")

POLL_UNTIL_S = 25 * 60  # :55 for an :30 as_of
NO_NEW_READ_S = 900.0
POLL_EVERY_S = 60.0
CONNECT_S = 120.0
KEEP_REFUSED = timedelta(hours=24)  # a table refusal; `refused.replay` brings it back after a fix
KEEP_ANY = timedelta(days=7)  # any other error, so a refusal no token names still leaves the log
REFUSALS = ("CONSTRAINT", "CAST_INVALID_INPUT", "UNRESOLVED_COLUMN")


@dataclass
class Hour:
    run_id: str
    as_of: datetime
    started_at: datetime
    hourly: bool
    args: argparse.Namespace
    seams: Any
    cache: Path
    log_path: Path
    budget: Budget
    skew: timedelta = field(default=timedelta(0))  # a `--seams` start time minus the wall clock

    def now(self) -> datetime:
        """The wall clock, which keeps counting while the Mac sleeps; monotonic time does not."""
        return datetime.now(UTC) + self.skew

    def base_row(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "as_of": self.as_of.isoformat(),
            "started_at": self.started_at.isoformat(),
            "route": "none",
        }


def profile() -> str:
    p = os.environ.get("WEATHER_SIGNAL_PROFILE", "")
    if not p:
        raise RuntimeError("WEATHER_SIGNAL_PROFILE is not set")
    return p


def connect_spark(p: str) -> Any:
    """A Connect serverless session on the named profile; nothing is resolved from the environment."""
    from weather_signal.agent.tracing import bounded
    from weather_signal.spark import connect_session

    return bounded(lambda: connect_session(p), CONNECT_S, "connect")


def names_for(h: Hour) -> Names:
    n = Names.from_env()
    if not h.hourly and not h.args.dry_run and n.prefix != "dev_":
        raise RuntimeError("an --as-of run writes only to dev_ names or under --dry-run")
    return n


def open_store(h: Hour) -> Store:
    """Fixture reads and writes; with `--eval-run`, fixture reads and the real MERGE into the eval table."""
    if h.args.fixture and not h.args.eval_run:
        return FileStore(Path(h.args.fixture))
    spark = SparkStore(connect_spark(profile()), names_for(h), eval_run=h.args.eval_run)
    return EvalStore(spark, Path(h.args.fixture)) if h.args.fixture else spark


def _read_clock(h: Hour) -> tuple[float, float]:
    """Seconds left to :55 and seconds since the start, both on `h.now()`."""
    now = h.now()
    left = (h.as_of + timedelta(seconds=POLL_UNTIL_S) - now).total_seconds()
    return left, (now - h.started_at).total_seconds()


def _next_wait(h: Hour) -> float | None:
    """Seconds to sleep before the next read, or `None` past :55 or once 900 s of the hour are gone."""
    left, elapsed = _read_clock(h)
    step = min(left, POLL_EVERY_S)
    return None if left <= 0 or elapsed + step > NO_NEW_READ_S else step


def poll(store: Store, h: Hour) -> dict[str, dict[str, Any]]:
    """`fn_agent_inputs(as_of)` until :55, with no new read once 900 s of the hour are gone."""
    while True:
        rows = store.inputs(h.as_of)
        wait = _next_wait(h)
        if rows or wait is None:
            return rows
        time.sleep(wait)


def await_guard(store: Store, h: Hour) -> str:
    """`v_guard_hold` read until this as_of is verified, an incident opens, or reads must stop."""
    while True:
        try:
            state = store.guard(h.as_of)
        except Exception as err:  # noqa: BLE001 - an unreadable guard holds the hour, as an incident does
            return f"unreadable ({error_text(err)})"
        wait = _next_wait(h)
        if state != GUARD_UNVERIFIED or wait is None:
            return state
        log.info("guard not yet verified for %s; reading again in %.0f s", h.as_of, wait)
        time.sleep(wait)


def reread(store: Store, h: Hour) -> tuple[dict[str, dict[str, Any]], str | None]:
    """The inputs once more after the verify, under the same 900 s and :55 limits as every read."""
    left, elapsed = _read_clock(h)
    if left < 0 or elapsed > NO_NEW_READ_S:
        return {}, "verified past the no-new-read limit"
    try:
        return store.inputs(h.as_of), None
    except Exception as err:  # noqa: BLE001 - held like an unreadable guard, not crashed
        return {}, f"unreadable ({error_text(err)})"


def user_turn(as_of: datetime, dates: list[str]) -> str:
    return f"as_of: {as_of.isoformat()}\ntarget_dates: {', '.join(dates)}"


def agent_version() -> str:
    from importlib.metadata import version

    return version("weather-signal")


def run_hour(h: Hour) -> int:
    row: dict[str, Any] = h.base_row()
    try:
        store = open_store(h)
        row["writer"] = store.writer()
    except Exception as err:  # noqa: BLE001 - a Connect failure is an outcome, not a crash
        return _end(h, None, {**row, "outcome": "connect_failed", "error": error_text(err)})
    if h.hourly:
        _reconcile(h, store)
    inputs = poll(store, h)
    if not inputs:
        return _end(h, store, {**row, "outcome": "signal_missing"})
    if h.hourly:
        guard = await_guard(store, h)
        if guard != GUARD_OK:
            return _end(
                h, store, {**row, "outcome": "guard_hold", "error": f"v_guard_hold: {guard}"}
            )
        inputs, why = reread(store, h)  # the verify may follow a re-score of this as_of
        if why is not None:
            return _end(
                h, store, {**row, "outcome": "guard_hold", "error": f"inputs re-read: {why}"}
            )
        if not inputs:
            return _end(h, store, {**row, "outcome": "signal_missing"})
    return _explain_and_write(h, store, inputs, row)


def _explain_and_write(
    h: Hour, store: Store, inputs: dict[str, dict[str, Any]], row: dict[str, Any]
) -> int:
    import anyio

    from weather_signal.agent.options import agent_cwd, options, prompt_sha
    from weather_signal.agent.tools import Session
    from weather_signal.agent.tracing import Tracer, tracking_uri

    session = Session(inputs=inputs, prices=store.prices(), as_of=h.as_of)
    version = agent_version()
    tracer = Tracer(h.seams.tracking_uri or tracking_uri(profile()))
    meta_tags = {
        "as_of": h.as_of.isoformat(),
        "run_id": h.run_id,
        "agent_version": version,
        "prompt_sha": prompt_sha(),
    }
    trace_id = tracer.start(h.run_id, meta_tags)
    opts = options(session, version=version, cwd=agent_cwd(h.cache), cli_path=h.seams.cli_path)
    caps = (h.seams.turn_s or GATEWAY_CAP_S, h.seams.turn_s or DIRECT_CAP_S)
    out = anyio.run(lambda: explain(opts, user_turn(h.as_of, session.dates), h.budget, caps=caps))
    outcome, missing = classify(out, len(session.dates), len(session.proposals))
    model = out.turn.model if out.turn and out.turn.model else "none"
    meta = {"llm_model_id": model, "trace_id": trace_id, "agent_version": version}
    binds = [
        bind(h.as_of, date.fromisoformat(d), p, meta=meta, llm_route=out.route)
        for d, p in fill(session.dates, session.proposals, missing=missing).items()
    ]
    written = _write(h, store, binds, out.route)
    row.update(_run_fields(out, outcome), **meta, prompt_sha=prompt_sha(), rows_written=written)
    row.update(downgrades=sum(1 for b in binds if b["downgrade"]), hook_denials=session.denials)
    tracer.finish(_span_tags(out), {"outcome": outcome, "rows_written": written})
    return _end(h, store, row)


def _write(h: Hour, store: Store, binds: list[dict[str, Any]], route: str) -> int:
    if h.args.dry_run:
        for b in binds:
            sys.stdout.write(f"{ {k: v for k, v in b.items() if k != 'as_of'} }\n")
        return 0
    return store.merge(binds, llm_route=route)


def _span_tags(out: Outcome) -> dict[str, str]:
    v = out.verdict
    return {
        "backend": getattr(v, "backend", None) or "none",
        "cli_version": getattr(v, "cli_version", None) or "none",
        "route": out.route,
    }


def _run_fields(out: Outcome, outcome: str) -> dict[str, Any]:
    from claude_agent_sdk import __version__ as sdk_version

    v, t = out.verdict, out.turn
    return {
        "outcome": outcome,
        "route": out.route,
        "fallback_reason": out.fallback_reason,
        "databricks_service": getattr(v, "databricks_service", None),
        "api_error_status": t.api_error_status if t else None,
        "refusal": out.refusal,
        "error": (t.error if t else None) or out.error,
        "backend": getattr(v, "backend", None),
        "cli_version": getattr(v, "cli_version", None),
        "api_provider": getattr(v, "api_provider", None),
        "subscription": getattr(v, "subscription", None),
        "sdk_version": sdk_version,
        "duration_ms": t.duration_ms if t else None,
        "duration_api_ms": t.duration_api_ms if t else None,
        "num_turns": t.num_turns if t else None,
        "cost_usd_equiv": t.cost_usd if t else None,
    }


def _reconcile(h: Hour, store: Store) -> None:
    rec = runlog.reconcile(h.log_path, h.cache, lambda ids: store.run_ids_present(ids) | {h.run_id})
    kept: set[str] = set()
    for r in rec.rows:
        try:
            store.insert_run(r)
        except Exception as err:  # noqa: BLE001 - the line stays for a later hour; this one runs
            keep = KEEP_REFUSED if _refused(err) else KEEP_ANY
            if _older(h, r, keep):
                refused.append(h.cache, {**r, "refused": error_text(err)})
                log.warning("run row %s not taken for %s; moved aside", r["run_id"], keep)
                continue
            kept.add(r["run_id"])
            log.warning("earlier run row %s not inserted (%s)", r["run_id"], error_text(err))
    for f in rec.deadline_files:
        if f not in {runlog.deadline_path(h.cache, rid) for rid in kept}:
            f.unlink(missing_ok=True)
    if rec.rows:
        log.info("reconciled %d earlier run rows", len(rec.rows) - len(kept))
    _drop_inserted(h, kept)


def _refused(err: BaseException) -> bool:
    """The table turned the row down, as against Connect or the warehouse being unreachable."""
    return any(m in str(err).upper() for m in REFUSALS)


def _older(h: Hour, row: dict[str, Any], keep: timedelta) -> bool:
    """The row's run started more than `keep` before this one."""
    try:
        started = datetime.fromisoformat(str(row.get("started_at")))
    except ValueError:
        return False
    return started.tzinfo is not None and h.started_at - started > keep


def _drop_inserted(h: Hour, kept: set[str]) -> None:
    """Keep this run's lines and any row the table refused; every other run id now has its row."""
    mine = [line for line in runlog.read(h.log_path) if line.get("run_id") in kept | {h.run_id}]
    tmp = h.log_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    tmp.touch()
    for line in mine:
        runlog.append(tmp, line)
    os.replace(tmp, h.log_path)


def _end(h: Hour, store: Store | None, row: dict[str, Any]) -> int:
    row["finished_at"] = datetime.now(UTC).isoformat()
    runlog.append(h.log_path, row)
    if store is None or h.args.dry_run:
        return 0
    try:
        store.insert_run(row)
    except Exception as err:  # noqa: BLE001 - the line stays in runs.jsonl for the next run
        log.warning("run row not inserted (%s); kept for the next run", error_text(err))
    return 0
