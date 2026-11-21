"""`weather-signal-intraday`, the ingest job's own task at :05: Yahoo 60m bars for display only."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from weather_signal import config
from weather_signal.ingest import intraday
from weather_signal.ingest.fetch import RequestsTransport
from weather_signal.ingest.files import Files
from weather_signal.sessions import load as load_calendar

log = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    """Always exits clean: a skipped hour is logged, and the next session hour refetches five days."""
    ap = argparse.ArgumentParser(prog="weather-signal-intraday")
    ap.add_argument("--catalog", default="workspace")
    ap.add_argument("--schema-prefix", default="")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    n = config.Names(a.catalog, a.schema_prefix)
    now = datetime.now(UTC)
    out = intraday.fetch_and_land(Files.from_sdk(), n, load_calendar(), RequestsTransport(), now)
    log.info("intraday %s at %s: %s", intraday.TICKER, now.isoformat(timespec="seconds"), out)
    return 0
