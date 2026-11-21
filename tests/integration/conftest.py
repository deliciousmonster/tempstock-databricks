"""Integration tests: the `dev` target over Databricks Connect serverless, never a warehouse or prod names."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

import pytest

from weather_signal.config import Names

PROFILE = os.environ.get("DATABRICKS_CONFIG_PROFILE", "weather-signal")
DEV = Names("workspace", "dev_")
CONNECT_TIMEOUT_S = 120
pytestmark = pytest.mark.integration


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def n() -> Names:
    assert DEV.prefix == "dev_", "integration tests never touch production names"
    return DEV


@pytest.fixture(scope="session")
def spark():
    connect = pytest.importorskip("databricks.connect", reason="needs the connect dependency group")

    def build():
        return connect.DatabricksSession.builder.profile(PROFILE).serverless().getOrCreate()

    with ThreadPoolExecutor(1) as ex:
        try:
            s = ex.submit(build).result(timeout=CONNECT_TIMEOUT_S)
        except FutureTimeout:
            pytest.skip(f"Connect serverless gave no session in {CONNECT_TIMEOUT_S} s")
    yield s


@pytest.fixture(scope="session")
def cat(spark, n):
    from weather_signal.guard.spark_catalog import SparkCatalog

    return SparkCatalog(spark, n)


@pytest.fixture
def run_id() -> str:
    return uuid.uuid4().hex[:8]


def cli(*args: str, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("databricks")
    assert exe, "databricks CLI not on PATH"
    env = {**os.environ, "DATABRICKS_CONFIG_PROFILE": PROFILE}
    return subprocess.run(
        [exe, *args], capture_output=True, text=True, timeout=timeout, env=env, check=False
    )


def warehouse_id() -> str:
    """The workspace's one SQL warehouse, found as bootstrap's preflight finds it."""
    out = cli("warehouses", "list", "-o", "json", timeout=120)
    assert out.returncode == 0, out.stderr
    rows = json.loads(out.stdout)
    assert len(rows) == 1, [r.get("name") for r in rows]
    return str(rows[0]["id"])


def sql_error(spark, q: str) -> str:
    """The error text a statement raises, or '' when it succeeds."""
    try:
        spark.sql(q).collect()
    except Exception as err:  # noqa: BLE001 - the test asserts on the text
        return str(err)
    return ""
