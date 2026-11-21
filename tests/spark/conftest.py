from __future__ import annotations

import json
import os
import time
from datetime import datetime

import pytest

# Collected timestamps come back naive in the driver's zone; pin it to UTC and compare naive.
os.environ["TZ"] = "UTC"
time.tzset()

pyspark = pytest.importorskip("pyspark")


@pytest.fixture(scope="session")
def spark(tmp_path_factory):
    from pyspark.sql import SparkSession

    # Keep Spark's default warehouse and metastore out of the checkout.
    scratch = tmp_path_factory.mktemp("spark")

    s = (
        SparkSession.builder.master("local[2]")
        .appName("weather-signal-tests")
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.warehouse.dir", str(scratch / "warehouse"))
        .config("spark.driver.extraJavaOptions", f"-Dderby.system.home={scratch}")
        .getOrCreate()
    )
    yield s
    s.stop()


@pytest.fixture(scope="session")
def metros(spark):
    import pandas as pd

    from tests.conftest import SEED

    m = pd.read_csv(SEED / "metros.csv", dtype={"cbsa_code": str})[["rank", "cbsa_code", "weight"]]
    return spark.createDataFrame(m)


def ts(*a: int) -> datetime:
    return datetime(*a)


def body(locations: list[dict]) -> str:
    return json.dumps(locations if len(locations) > 1 else locations[0])
