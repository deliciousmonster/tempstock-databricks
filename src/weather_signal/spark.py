"""Spark sessions: the job's own on serverless, or Databricks Connect from the Mac in a UTC process."""

from __future__ import annotations

import os
import time
from typing import Any


def pin_utc() -> None:
    """Connect collects and binds naive TIMESTAMPs in the process zone, which no session conf reaches."""
    os.environ["TZ"] = "UTC"
    time.tzset()


def connect_session(profile: str | None) -> Any:
    """A serverless session on `profile`, or on the default Connect config when it is `None`."""
    from databricks.connect import DatabricksSession

    pin_utc()
    b = DatabricksSession.builder
    return (b.profile(profile) if profile else b).serverless().getOrCreate()


def spark_session(profile: str | None = None) -> Any:
    """The job's own session on serverless; Databricks Connect serverless from the Mac."""
    if "DATABRICKS_RUNTIME_VERSION" in os.environ and profile is None:
        from pyspark.sql import SparkSession

        return SparkSession.builder.getOrCreate()
    return connect_session(profile)
