"""Every log line is in UTC and says so, whatever the process zone."""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime

from weather_signal.agent import worker


def test_a_line_written_in_denver_reads_utc_with_z():
    before = os.environ.get("TZ")
    os.environ["TZ"] = "America/Denver"
    time.tzset()
    try:
        rec = logging.LogRecord("weather_signal.agent", logging.INFO, "", 0, "hi", None, None)
        rec.created = datetime(2026, 11, 19, 11, 40, 5, 250000, tzinfo=UTC).timestamp()
        rec.msecs = 250.0
        line = worker.log_formatter().format(rec)
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()
    assert line == "2026-11-19T11:40:05.250Z INFO weather_signal.agent hi"
