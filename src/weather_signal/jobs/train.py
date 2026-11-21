"""`weather-signal-train`: register the walk's latest refit, `@champion` only if its gate passed."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from weather_signal import config
from weather_signal.jobs.backtest import backtest_inputs
from weather_signal.sessions import load as load_calendar
from weather_signal.signal.train import MODEL, MlflowRegistry, train
from weather_signal.spark import spark_session

log = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="weather-signal-train")
    ap.add_argument("--catalog", default="workspace")
    ap.add_argument("--schema-prefix", default="")
    ap.add_argument("--profile", help="Databricks Connect profile, from the Mac")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    n = config.Names(a.catalog, a.schema_prefix)
    reg = MlflowRegistry(f"/Shared/weather-signal-{a.schema_prefix}train")
    inp = backtest_inputs(spark_session(a.profile), n)
    out = train(inp, load_calendar(), reg, n.table("signals", MODEL), datetime.now(UTC))
    log.info("train %s: %s", n.table("signals", MODEL), out)
    return 0
