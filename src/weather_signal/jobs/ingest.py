"""`weather-signal-ingest` at :05, and the Mac's :20 weather fallback: fetch, validate, land once."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime
from typing import Any

from weather_signal import config
from weather_signal.ingest import av_ledger as av
from weather_signal.ingest import catchup, hourly
from weather_signal.ingest.fetch import RequestsTransport
from weather_signal.ingest.files import Files
from weather_signal.sessions import load as load_calendar

log = logging.getLogger(__name__)


class CatchUpFailed(RuntimeError):
    """A catch-up, asked for or at hourly.ERA5_HOUR, with a failed chunk or ticker; the run ends FAILED."""


def _secret(scope: str) -> str:
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient().dbutils.secrets.get(scope, config.SECRET_KEY)


def main(argv: list[str] | None = None) -> int:
    import time

    ap = argparse.ArgumentParser(prog="weather-signal-ingest")
    ap.add_argument("--catalog", default="workspace")
    ap.add_argument("--schema-prefix", default="")
    ap.add_argument("--p-hours", type=int, default=config.P_PROVISIONAL_H)
    ap.add_argument("--secret-scope", default=config.SECRET_SCOPE)
    ap.add_argument("--skip-alphavantage", action="store_true")
    ap.add_argument(
        "--catch-up", choices=["true", "false"], default="false",
        help="fetch each source from its newest landed slot to now; also runs at ERA5_HOUR",
    )  # fmt: skip
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    n = config.Names(a.catalog, a.schema_prefix)
    files, transport, cal = Files.from_sdk(), RequestsTransport(), load_calendar()
    now = datetime.now(UTC)
    report: dict[str, Any] = {
        "previous_runs": hourly.weather_hour(files, n, now, transport, a.p_hours)
    }
    if now.hour == hourly.ERA5_HOUR:
        report["era5"] = hourly.era5_day(files, n, now, transport)
    if not a.skip_alphavantage:
        clock = av.Clock(lambda: datetime.now(UTC), time.sleep)
        rep = hourly.alpha_vantage_slot(files, n, cal, _secret(a.secret_scope), transport, clock)
        report["alphavantage"] = {
            "ticker": rep.ticker,
            "carried": rep.carried,
            "outcome": rep.outcome,
        }
    code = 0
    if a.catch_up == "true" or now.hour == hourly.ERA5_HOUR:
        cu = catchup.run(files, n, transport, cal, now, lambda: datetime.now(UTC), time.sleep)
        report["catch_up"] = cu.__dict__
        code = 1 if cu.failed else 0
    log.info("ingest %s", json.dumps(report))
    if code:
        # A wheel task ignores the return value; only a raise fails the run.
        raise CatchUpFailed(f"catch-up: {report['catch_up']['failed']} failed")
    return code
