"""A planted hour on `dev`, real Haiku on the gateway route, the reader unpatched. Spends 1 gateway turn."""

import json
import os
import subprocess
import sys
import uuid

from tests.integration.agent.conftest import GATEWAY_ENV
from tests.integration.helpers import frame, scratch_as_of
from weather_signal.agent.scorers import no_contradiction
from weather_signal.agent.store import SparkStore
from weather_signal.jobs.score import write_signal


def test_end_to_end(live, spark, n, tmp_path):
    as_of = scratch_as_of(f"e2e-{uuid.uuid4().hex}")
    rows = [{**r, "action": "sell" if i % 2 else "buy"} for i, r in enumerate(frame(as_of)[:7])]
    write_signal(spark, n, rows)
    seams = tmp_path / "seams.json"
    # Traces to a local file store: the brief allows workspace writes only into dev_ schemas.
    mlruns = (tmp_path / "mlruns").as_uri()
    seams.write_text(json.dumps({"cache_dir": str(tmp_path / "cache"), "tracking_uri": mlruns}))
    env = {
        **os.environ,
        **GATEWAY_ENV,
        "WEATHER_SIGNAL_PROFILE": "weather-signal",
        "WEATHER_SIGNAL_SCHEMA_PREFIX": "dev_",
        "MLFLOW_ALLOW_FILE_STORE": "true",
    }
    assert SparkStore(spark, n).inputs(as_of), "the reader sees no inputs; no turn spent"
    cmd = [sys.executable, "-m", "weather_signal.agent", "run", "--as-of", as_of.isoformat()]
    cmd += ["--seams", str(seams)]
    assert subprocess.run(cmd, env=env, timeout=900, check=False).returncode == 0
    got = [
        r.asDict()
        for r in spark.sql(
            f"SELECT * FROM {n.table('signals', 'recommendations')} WHERE as_of = :a",
            args={"a": as_of},
        ).collect()
    ]
    assert len(got) == len(rows)
    assert all(r["trace_id"] for r in got)
    assert any(r["action"] != "hold" and r["downgrade_reason"] is None for r in got)
    assert all(no_contradiction(r["rationale"] or "", r["action"]) for r in got)
    (line,) = [
        x
        for x in map(json.loads, (tmp_path / "cache" / "runs-eval.jsonl").open())
        if "outcome" in x
    ]
    assert line["route"] == "databricks-gateway" and line["outcome"] in ("ok", "partial")
    assert {r["trace_id"] for r in got} == {line["trace_id"]} and line["trace_id"].startswith("tr-")
    (tmp_path / "row.json").write_text(json.dumps(line, default=str))
