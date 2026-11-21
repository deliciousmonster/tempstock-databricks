"""The route a real hour records: a service that doesn't exist goes direct; the test service stays.

Spends 1 direct turn (the forced fallback) and 1 gateway turn. Inputs from a fixture, no Delta write.
"""

import json
import os
import subprocess
import sys

import pytest

from tests.integration.agent.conftest import GATEWAY_ENV
from tests.process.agent.harness import AS_OF, hour

REPO = __import__("pathlib").Path(__file__).resolve().parents[3]


def gateway_token() -> str:
    from databricks.sdk.core import Config

    return Config(profile="weather-signal").authenticate()["Authorization"].split()[-1]


def run(tmp_path, service):
    fixture = tmp_path / f"{service}.json"
    fixture.write_text(json.dumps({**hour(), "out": str(tmp_path / service)}), encoding="utf-8")
    seams = tmp_path / "seams.json"
    seams.write_text(
        json.dumps(
            {"cache_dir": str(tmp_path / "cache"), "tracking_uri": (tmp_path / "mlruns").as_uri()}
        )
    )
    env = {
        **os.environ,
        **GATEWAY_ENV,
        "CREW_DATABRICKS_SERVICE": service,
        "WEATHER_SIGNAL_PROFILE": "weather-signal",
    }
    env["MLFLOW_ALLOW_FILE_STORE"] = "true"
    cmd = [
        sys.executable,
        "-m",
        "weather_signal.agent",
        "run",
        "--as-of",
        AS_OF.isoformat(),
        "--fixture",
        str(fixture),
        "--seams",
        str(seams),
    ]
    p = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=600, check=False, cwd=REPO
    )
    lines = [
        json.loads(x) for x in (tmp_path / "cache" / "runs-eval.jsonl").read_text().splitlines()
    ]
    return p, lines[-1]


@pytest.mark.parametrize(
    ("service", "route", "reason"),
    [
        (
            "workspace.default.weather_signal_no_such_service",
            "direct_fallback",
            "databricks_gateway_service_unknown",
        ),
        ("workspace.default.claude_subscription", "databricks-gateway", None),
    ],
)
def test_route_recorded(live, tmp_path, service, route, reason):
    p, row = run(tmp_path, service)
    assert p.returncode == 0
    assert (row["route"], row["fallback_reason"]) == (route, reason)
    assert row["outcome"] in ("ok", "partial")
    token = gateway_token()
    blobs = [p.stderr, json.dumps(row)]
    blobs += [
        f.read_text(errors="replace") for f in (tmp_path / "mlruns").rglob("*") if f.is_file()
    ]
    for b in blobs:
        assert token not in b and token[-16:] not in b
