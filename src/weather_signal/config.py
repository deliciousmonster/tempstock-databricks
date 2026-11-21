"""Names and constants shared by every job, the CLI and the tests."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date

P_PROVISIONAL_H = 30
PRIMARY = "WMT"
BENCHMARKS = ("SPY", "XLP")
TICKERS = (PRIMARY, *BENCHMARKS)
# Feature keys name the primary by role, so a change of stock touches this file alone.
PRIMARY_KEY = "stock"
MODEL_NAME = f"{PRIMARY_KEY}_signal"
HISTORY_START = date(1991, 1, 1)
# A benchmark listed after HISTORY_START: its history starts at its first session
BENCHMARK_FIRST = {"SPY": date(1993, 1, 29), "XLP": date(1998, 12, 22)}
SECRET_SCOPE = "weather-signal"
SECRET_KEY = "alphavantage-api-key"
# The workspace Git folder bootstrap keeps on GIT_FOLDER_BRANCH; a new remote changes this line only.
GIT_FOLDER_URL = "https://github.com/deliciousmonster/tempstock-databricks.git"
GIT_FOLDER_BRANCH = "main"


def key(ticker: str) -> str:
    """The feature-key stem for a ticker: `stock` for the primary, the lower-cased ticker otherwise."""
    return PRIMARY_KEY if ticker == PRIMARY else ticker.lower()


@dataclass(frozen=True)
class Names:
    """Fully qualified Unity Catalog names for one target (prod or a dev prefix)."""

    catalog: str = "workspace"
    prefix: str = ""

    def schema(self, name: str) -> str:
        return f"{self.catalog}.{self.prefix}{name}"

    def table(self, schema: str, name: str) -> str:
        return f"{self.schema(schema)}.{name}"

    def volume_path(self, schema: str, volume: str) -> str:
        return f"/Volumes/{self.catalog}/{self.prefix}{schema}/{volume}"

    @property
    def raw(self) -> str:
        return self.volume_path("raw", "raw")

    @property
    def ledger(self) -> str:
        return self.volume_path("raw", "ledger")

    @property
    def guarded(self) -> tuple[str, str]:
        return (self.table("signals", "signal"), self.table("signals", "recommendations"))

    @classmethod
    def from_env(cls) -> Names:
        return cls(
            catalog=os.environ.get("WEATHER_SIGNAL_CATALOG", "workspace"),
            prefix=os.environ.get("WEATHER_SIGNAL_SCHEMA_PREFIX", ""),
        )
