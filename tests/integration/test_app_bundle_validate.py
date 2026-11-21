"""`bundle validate` passes for the App bundle on both targets, as it resolves them."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.integration.conftest import PROFILE

APP = Path(__file__).resolve().parents[2] / "app"


def validate(target: str) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("databricks")
    assert exe, "databricks CLI not on PATH"
    env = {**os.environ, "DATABRICKS_CONFIG_PROFILE": PROFILE}
    args = [exe, "bundle", "validate", "-t", target, "-o", "json"]
    return subprocess.run(
        args, cwd=APP, capture_output=True, text=True, timeout=300, env=env, check=False
    )


@pytest.mark.parametrize(
    ("target", "name", "signals"),
    [("dev", "dev-weather-signal-ui", "dev_signals")],
)
def test_validate(target, name, signals):
    r = validate(target)
    assert r.returncode == 0, r.stderr[-800:]
    doc = json.loads(r.stdout)
    app = doc["resources"]["apps"]["ui_app"]
    assert app["name"] == name and "run_as" not in doc
    assert {e["name"]: e.get("value") for e in app["config"]["env"]}[
        "WS_UI_SIGNALS_SCHEMA"
    ] == signals
