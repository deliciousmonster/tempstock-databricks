"""What every wheel task shares: the one failure type and the common arguments."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from weather_signal import config


class TaskFailed(RuntimeError):
    """The task's one failure reason: `guard_incident`, `no_champion` or `no_signal_rows`."""


def task_args(prog: str, argv: Sequence[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog=prog)
    ap.add_argument("--catalog", default="workspace")
    ap.add_argument("--schema-prefix", default="")
    ap.add_argument("--p-hours", type=int, default=config.P_PROVISIONAL_H)
    ap.add_argument(
        "--as-of", help="ISO time, snapped to its hour's :30; the job passes its trigger time"
    )
    ap.add_argument("--profile", help="Databricks Connect profile, from the Mac")
    return ap.parse_args(argv)
