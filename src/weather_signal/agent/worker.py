"""`python -m weather_signal.agent run`: one hour, start to `os._exit`.

Order: launch check, `started` line, deadline, late-start check, Connect and reads, crewkit `connect`
(gateway, then direct only on a pre-check failure), Claude, MERGE per date, run row, exit.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

from weather_signal.agent import runlog
from weather_signal.agent.budget import HARD_S, Budget
from weather_signal.agent.deadline import Deadline, finish
from weather_signal.agent.hour import Hour, run_hour
from weather_signal.agent.options import CACHE
from weather_signal.agent.route import error_text

log = logging.getLogger("weather_signal.agent")

LABEL = "dev.weather-signal.agent"
LATE_S = 20 * 60
NOT_LAUNCHD = f"not under launchd: use launchctl kickstart gui/$(id -u)/{LABEL}"


@dataclass(frozen=True)
class Seams:
    """Test injections, read from `--seams <json>`; the hourly agent under launchd passes none."""

    deadline_s: float = HARD_S
    clock_offset_s: float = 0.0
    turn_s: float | None = None
    cli_path: str | None = None
    cache_dir: str | None = None
    tracking_uri: str | None = None
    now: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | None) -> Seams:
        if not path:
            return cls()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        known = {
            k: data.pop(k) for k in list(data) if k in cls.__dataclass_fields__ and k != "extra"
        }
        return cls(**known, extra=data)


def parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m weather_signal.agent run")
    p.add_argument("--as-of", help="explicit HH:30 in UTC; skips the launch and late-start checks")
    p.add_argument("--dry-run", action="store_true", help="read and call Claude, write nothing")
    p.add_argument("--fixture", help="read inputs from this JSON file instead of Unity Catalog")
    p.add_argument("--eval-run", help="write into recommendations_eval under this id (dev only)")
    p.add_argument("--seams", help=argparse.SUPPRESS)
    return p.parse_args(argv)


def as_of_for(now: datetime) -> datetime:
    """The latest HH:30 at or before `now`."""
    t = now.astimezone(UTC).replace(second=0, microsecond=0)
    t = t.replace(minute=30) if t.minute >= 30 else t.replace(minute=30) - timedelta(hours=1)
    return t


def parse_as_of(text: str) -> datetime:
    t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    t = t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)
    if (t.minute, t.second, t.microsecond) != (30, 0, 0):
        raise SystemExit("--as-of must be an HH:30 in UTC")
    return t


def log_formatter() -> logging.Formatter:
    """UTC with an explicit `Z`, whatever the process zone is when a line is written."""
    f = logging.Formatter(
        "%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S"
    )
    f.converter = time.gmtime
    return f


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(log_formatter())
    level = os.environ.get("WEATHER_SIGNAL_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, handlers=[handler])


def main(argv: list[str] | None = None) -> NoReturn:
    args = parse(argv)
    os.environ["MLFLOW_DISABLE_TELEMETRY"] = "true"  # MLflow otherwise posts usage to its own host
    seams = Seams.load(args.seams)
    hourly = args.as_of is None
    if hourly and os.environ.get("XPC_SERVICE_NAME") != LABEL:
        sys.stderr.write(NOT_LAUNCHD + "\n")
        finish(2, None)
    setup_logging()
    wall = datetime.now(UTC)
    started = datetime.fromisoformat(seams.now) if seams.now else wall
    as_of = as_of_for(started) if hourly else parse_as_of(args.as_of)
    cache = Path(seams.cache_dir) if seams.cache_dir else CACHE
    cache.mkdir(parents=True, exist_ok=True)
    os.chdir(cache)  # MLflow's Claude autolog writes `.claude/mlflow/` under the cwd
    h = Hour(
        run_id=uuid.uuid4().hex,
        as_of=as_of,
        started_at=started,
        hourly=hourly,
        args=args,
        seams=seams,
        cache=cache,
        log_path=cache / (runlog.HOURLY if hourly else runlog.EVAL),
        budget=Budget(time.monotonic(), offset_s=seams.clock_offset_s),
        skew=started - wall,
    )
    runlog.append(h.log_path, {"line": "started", **h.base_row()})
    deadline = Deadline(runlog.deadline_path(cache, h.run_id))
    try:
        deadline.arm(seams.deadline_s)
    except OSError as err:
        log.error("deadline not armed: %s", error_text(err))
        finish(1, None)
    if hourly and (started - as_of).total_seconds() > LATE_S:
        runlog.append(h.log_path, {**h.base_row(), "outcome": "late_start", "route": "none"})
        log.info("late start for %s; the next :40 takes the next hour", as_of.isoformat())
        finish(0, deadline)
    finish(_guarded(h), deadline)


def _guarded(h: Hour) -> int:
    try:
        return run_hour(h)
    except Exception as err:  # noqa: BLE001 - recorded as `crashed`, then the exit sequence
        log.error("run crashed: %s", error_text(err))
        row = {**h.base_row(), "outcome": "crashed", "route": "none", "error": error_text(err)}
        runlog.append(h.log_path, row)
        return 1
