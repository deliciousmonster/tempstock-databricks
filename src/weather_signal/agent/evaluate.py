"""The stage 5 evaluation: twelve fixture hours, three runs each, scored and put through `bar.verdict`.

    uv run --extra agent python -m weather_signal.agent.evaluate [--runs 3] [--judge] [--previous verdict.json]

36 Haiku turns on the gateway route through `workspace.default.claude_subscription`; each run writes
through the real MERGE into `dev_signals.recommendations_eval` under one `eval_run_id`. The judge
(`system.ai.gpt-oss-120b` through the AI Gateway) is reported beside the bar, never gating.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from weather_signal.agent import scorers
from weather_signal.agent.bar import RunScore, Verdict, verdict
from weather_signal.config import Names

SERVICE = "workspace.default.claude_subscription"
JUDGE_MODEL = "system.ai.gpt-oss-120b"
GUIDELINE = (
    "The rationale explains the signal and does not advise; it names the input it relied on."
)
FAULTS = ("clean", "stale_inputs", "out_of_range", "inconsistent_inputs")
BASE = datetime(2014, 6, 2, 14, 30, tzinfo=UTC)


def fixture_hour(fault: str, k: int) -> tuple[datetime, dict[str, Any]]:
    """Seven dates, the fault just past its threshold: hour-level for stale and `ret_5d`, per date otherwise."""
    as_of = BASE + timedelta(days=FAULTS.index(fault) * 3 + k)
    inputs = {}
    for i in range(7):
        d = (as_of.date() + timedelta(days=i)).isoformat()
        inputs[d] = {
            "signal_action": "hold" if i == 6 else ("sell" if i % 2 else "buy"),
            "score": round(0.41 - i / 10, 2),
            "signal_confidence": 0.6,
            "reasons": ["anomaly"],
            "horizon_days": i,
            "inputs_max_known_at": (as_of - timedelta(hours=1)).isoformat(),
            "forecast_known_at": (as_of - timedelta(hours=2)).isoformat(),
            "index_c": 4.0 + i,
            "normal_c": 5.0,
            "anomaly_c": -1.0 + i / 2,
            "last_close": 187.25,
            "ret_5d": -0.02,
        }
    for i, row in enumerate(inputs.values()):
        if fault == "stale_inputs":
            row["forecast_known_at"] = (as_of - scorers.STALE - timedelta(minutes=5)).isoformat()
        elif fault == "inconsistent_inputs":
            row["ret_5d"] = 0.27
        elif fault == "out_of_range" and i in (1, 3 + k % 2):
            row["anomaly_c"] = 12.5
    prices = [
        {"trade_date": (as_of.date() - timedelta(days=d)).isoformat(), "close": 185 + d / 3}
        for d in range(20, 0, -1)
    ]
    return as_of, {"inputs": inputs, "prices": prices, "writer": "eval"}


def plant_signal(spark: Any, n: Names, as_of: datetime, hour: dict[str, Any]) -> None:
    from tests.integration.helpers import frame

    from weather_signal.jobs.score import write_signal

    rows = {r["target_date"].isoformat(): r for r in frame(as_of)}
    write_signal(
        spark,
        n,
        [{**rows[d], "action": v["signal_action"]} for d, v in hour["inputs"].items() if d in rows],
    )


def run_worker(as_of: datetime, fixture: Path, eval_run: str) -> int:
    env = {
        **os.environ,
        "CREW_LLM_BACKEND": "plan",
        "CREW_LLM_ROUTE": "databricks-gateway",
        "CREW_DATABRICKS_PROFILE": "weather-signal",
        "CREW_DATABRICKS_SERVICE": SERVICE,
        "WEATHER_SIGNAL_SCHEMA_PREFIX": "dev_",
    }
    cmd = [sys.executable, "-m", "weather_signal.agent", "run", "--as-of", as_of.isoformat()]
    cmd += ["--fixture", str(fixture), "--eval-run", eval_run]
    return subprocess.run(cmd, env=env, check=False, timeout=1500).returncode


def written(spark: Any, n: Names, eval_run: str, as_of: datetime) -> list[dict[str, Any]]:
    q = f"SELECT * FROM {n.table('signals', 'recommendations_eval')} WHERE eval_run_id = :e AND as_of = :a"
    return [r.asDict() for r in spark.sql(q, args={"e": eval_run, "a": as_of}).collect()]


def score_run(
    fault: str, k: int, as_of: datetime, hour: dict[str, Any], rows: list[dict[str, Any]]
) -> RunScore:
    misleading = cited = 0
    ok = True
    for r in rows:
        src = hour["inputs"][r["target_date"].isoformat()]
        want = scorers.faults(src, as_of)
        text = r.get("rationale") or ""
        clean_ok = bool(want) or scorers.no_downgrade_clean(r)
        misleading += int(not scorers.no_contradiction(text, r["action"]) or not clean_ok)
        cited += int(
            scorers.cites_numbers(text, [v for v in src.values() if isinstance(v, int | float)])
        )
        if want:
            ok = ok and scorers.downgrade_matches_fault(r, want)
    return RunScore(f"{fault}-{k}", fault != "clean", ok, misleading, len(rows), cited)


def judge(rationales: list[str]) -> list[bool]:
    """One guidelines verdict per run's rationales, on the open-weight model through the gateway."""
    import requests
    from databricks.sdk.core import Config

    cfg = Config(profile="weather-signal")
    url = f"{cfg.host}/ai-gateway/anthropic/v1/messages"
    out = []
    for text in rationales:
        body = {
            "model": JUDGE_MODEL,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": f"{GUIDELINE}\nAnswer yes or no.\n\n{text}"}],
        }
        r = requests.post(url, json=body, headers=cfg.authenticate(), timeout=60)
        out.append(r.ok and "yes" in json.dumps(r.json()).lower())
    return out


def _args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--judge", action="store_true")
    p.add_argument("--previous", help="the first evaluation's verdict.json, for a whole rerun")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = _args(argv)
    from weather_signal.spark import connect_session

    spark = connect_session("weather-signal")  # pins the process zone to UTC first
    n = Names("workspace", "dev_")
    if not spark.catalog.tableExists(n.table("signals", "recommendations_eval")):
        sys.stderr.write("refused: dev_signals.recommendations_eval does not exist; run setup\n")
        return 2
    eval_run = uuid.uuid4().hex
    runs: list[RunScore] = []
    rationales: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for fault in FAULTS:
            for k in range(3):
                as_of, hour = fixture_hour(fault, k)
                plant_signal(spark, n, as_of, hour)
                fixture = Path(tmp) / f"{fault}-{k}.json"
                fixture.write_text(json.dumps({**hour, "out": tmp}), encoding="utf-8")
                for r in range(a.runs):
                    run_id = f"{eval_run}-{fault}-{k}-{r}"  # the eval table's key starts with it
                    run_worker(as_of, fixture, run_id)
                    rows = written(spark, n, run_id, as_of)
                    runs.append(score_run(fault, k, as_of, hour, rows))
                    rationales.append("\n".join(r.get("rationale") or "" for r in rows))
    prev = (
        Verdict(**json.loads(Path(a.previous).read_text(encoding="utf-8"))) if a.previous else None
    )
    v = verdict(runs, previous=prev)
    report: dict[str, Any] = {
        "eval_run_id": eval_run,
        "verdict": asdict(v),
        "runs": [asdict(r) for r in runs],
    }
    if a.judge:
        report["judge_pass"] = sum(judge(rationales))
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if v.status == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
