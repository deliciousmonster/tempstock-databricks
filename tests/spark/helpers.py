"""Envelope frames for the transforms, built from recorded payloads."""

from __future__ import annotations

import json
from datetime import datetime

from pyspark.sql import DataFrame, SparkSession


def envelopes(
    spark: SparkSession, items: list[tuple[str, datetime, object]], slot: str | None = None
) -> DataFrame:
    """One frame of envelopes; `slot` is the landing slot, a span `a..b` for a backfill chunk."""
    rows = [(p, t, json.dumps(b), slot) for p, t, b in items]
    return spark.createDataFrame(
        rows, "path string, fetched_at timestamp, body_text string, slot string"
    )
