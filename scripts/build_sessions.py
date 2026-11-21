"""Generate the packaged seed/sessions.csv from exchange_calendars' XNYS rules, past its end.

Run yearly: `uv run python scripts/build_sessions.py`. Holidays past the exchange's published years
come from the library's rules and can be wrong.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import exchange_calendars as xcals

START, END = "1990-01-01", "2035-12-31"
SESSIONS = Path(__file__).resolve().parents[1] / "src/weather_signal/seed/sessions.csv"


def build(out: Path, start: str = START, end: str = END) -> int:
    cal = xcals.get_calendar("XNYS", start=start, end=end)
    sched = cal.schedule
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["session_date", "open_utc", "close_utc", "end_date"])
        for day, row in sched.iterrows():
            w.writerow(
                [
                    day.date().isoformat(),
                    row["open"].tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S"),
                    row["close"].tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S"),
                    end,
                ]
            )
    return len(sched)


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else SESSIONS
    sys.stdout.write(f"{build(target)} sessions to {target}\n")
