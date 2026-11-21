"""The worker as a real child: a fake `claude`, a local gateway on 127.0.0.1, a fixture hour and a canary token."""

from __future__ import annotations

import base64
import http.server
import json
import os
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

FAKE = Path(__file__).with_name("fakecli.py")
DRIVERS = Path(__file__).with_name("drivers.py")
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "agent"
LABEL = "dev.weather-signal.agent"
AS_OF = datetime(2026, 11, 18, 14, 30, tzinfo=UTC)
SERVICE = "workspace.default.claude_subscription"


def _b64(obj: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


CANARY = ".".join(
    [
        _b64({"alg": "none", "typ": "JWT"}),
        _b64({"exp": 4102444800, "sub": "agent-canary"}),
        "Q2FuYXJ5U2lnbmF0dXJlU2VnbWVudDE",
    ]
)


def hour(as_of: datetime = AS_OF, *, n: int = 7) -> dict[str, Any]:
    """A clean fixture hour: seven dates, every signal `sell` except the last, which is `hold`."""
    inputs = {}
    for i in range(n):
        d = (as_of.date() + timedelta(days=i)).isoformat()
        inputs[d] = {
            "signal_action": "hold" if i == n - 1 else "sell",
            "score": round(-0.42 - i / 100, 2),
            "signal_confidence": 0.62,
            "reasons": ["anomaly_cold"],
            "horizon_days": i,
            "inputs_max_known_at": (as_of - timedelta(hours=1)).isoformat(),
            "forecast_known_at": (as_of - timedelta(hours=2)).isoformat(),
            "index_c": 3.1,
            "normal_c": 5.2,
            "anomaly_c": -2.1,
            "last_close": 187.25,
            "ret_5d": -0.031,
        }
    prices = [{"trade_date": f"2026-11-{d:02d}", "close": 180 + d / 2} for d in range(1, 18)]
    return {"inputs": inputs, "prices": prices, "writer": "fixture"}


class Gateway:
    """The two reads crewkit's pre-checks make; `mode` is relayed, direct, echo401 or hang."""

    def __init__(self) -> None:
        self.mode = "relayed"
        self.requests: list[str] = []
        gw = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                return

            def do_GET(self) -> None:
                gw.requests.append(self.path)
                status, body = gw.answer(self.path, dict(self.headers))
                if status == 0:
                    time.sleep(30)
                    return
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.host = f"http://127.0.0.1:{self.server.server_address[1]}"

    def answer(self, path: str, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        if path.startswith("/.well-known"):
            return 404, {}
        if "model-provider-services" in path:
            if self.mode == "hang":
                return 0, {}
            if self.mode == "echo401":
                return 401, {"error": "unauthorized", "echo": headers}
            body = json.loads(
                (FIXTURES / "databricks" / "provider-service-relayed.json").read_text()
            )
            if self.mode == "direct":
                body["config"]["anthropic"] = {"direct": {}}
            return 200, body
        if path.endswith("/v1/models"):
            return 200, json.loads((FIXTURES / "databricks" / "models.json").read_text())
        return 404, {}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@dataclass
class Result:
    rc: int
    elapsed: float


class World:
    def __init__(self, root: Path, gateway: Gateway, *, token: str = CANARY) -> None:
        self.root, self.gw = root, gateway
        self.cache, self.home, self.store = root / "cache", root / "home", root / "store"
        for d in (self.cache, self.home, self.store):
            d.mkdir(parents=True, exist_ok=True)
        self.cli = root / "claude"
        self.cli.write_text(
            f"#!{sys.executable}\nimport os, runpy\nos.environ['FAKE_DIR'] = {str(root)!r}\n"
            f"runpy.run_path({str(FAKE)!r}, run_name='__main__')\n",
            encoding="utf-8",
        )
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IXUSR)
        self.cfg = root / "databrickscfg"
        self.cfg.write_text(f"[gw]\nhost = {gateway.host}\ntoken = {token}\n", encoding="utf-8")
        self.fixture = root / "hour.json"
        self.set_hour(hour())
        self.log = root / "agent.log"
        self.seams: dict[str, Any] = {
            "cli_path": str(self.cli),
            "cache_dir": str(self.cache),
            "tracking_uri": (root / "mlruns").as_uri(),
        }
        self.cue([{}])

    def set_hour(self, data: dict[str, Any]) -> None:
        self.fixture.write_text(json.dumps({**data, "out": str(self.store)}), encoding="utf-8")

    def cue(self, spawns: list[dict[str, Any]]) -> None:
        (self.root / "cue.json").write_text(json.dumps({"spawns": spawns}), encoding="utf-8")

    def env(self, *, hourly: bool) -> dict[str, str]:
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "TMPDIR": str(self.root),
            "LANG": "C.UTF-8",
            "CREW_LLM_BACKEND": "plan",
            "CREW_LLM_ROUTE": "databricks-gateway",
            "CREW_DATABRICKS_PROFILE": "gw",
            "CREW_DATABRICKS_SERVICE": SERVICE,
            "DATABRICKS_CONFIG_FILE": str(self.cfg),
            "WEATHER_SIGNAL_PROFILE": "gw",
            "WEATHER_SIGNAL_LOG_LEVEL": "DEBUG",
            "MLFLOW_DISABLE_AGENT_HINT": "1",
            "MLFLOW_ALLOW_FILE_STORE": "true",
            "PYTHONUNBUFFERED": "1",
        }
        if "PYTHONPATH" in os.environ:  # the worker imports what this run imports, base or branch
            env["PYTHONPATH"] = os.environ["PYTHONPATH"]
        if hourly:
            env["XPC_SERVICE_NAME"] = LABEL
        return env

    def command(self, *, hourly: bool, driver: str | None = None) -> list[str]:
        seams = self.root / "seams.json"
        seams.write_text(json.dumps(self.seams), encoding="utf-8")
        head = (
            [sys.executable, str(DRIVERS), driver]
            if driver
            else [sys.executable, "-m", "weather_signal.agent"]
        )
        cmd = [*head, "run", "--fixture", str(self.fixture), "--seams", str(seams)]
        return cmd if hourly else [*cmd, "--as-of", AS_OF.isoformat()]

    def run(
        self,
        *,
        hourly: bool = False,
        timeout: float = 60,
        driver: str | None = None,
        env: dict[str, str] | None = None,
        new_session: bool = False,
    ) -> Result:
        if hourly and "now" not in self.seams:
            self.seams["now"] = (AS_OF + timedelta(minutes=10)).isoformat()
        t0 = time.monotonic()
        with self.log.open("ab") as err:
            p = subprocess.run(
                self.command(hourly=hourly, driver=driver),
                env={**self.env(hourly=hourly), **(env or {})},
                stdout=err,
                stderr=err,
                timeout=timeout,
                check=False,
                cwd=self.root,
                start_new_session=new_session,
            )
        return Result(p.returncode, time.monotonic() - t0)

    def fake(self) -> list[dict[str, Any]]:
        path = self.root / "fake.log"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def spawns(self) -> int:
        return sum(1 for e in self.fake() if "spawn" in e)

    def frames(self) -> list[str]:
        return [e["frame"] for e in self.fake() if "frame" in e]

    def lines(self, name: str = "runs-eval.jsonl") -> list[dict[str, Any]]:
        path = self.cache / name
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def run_row(self, name: str = "runs-eval.jsonl") -> dict[str, Any]:
        return next(x for x in self.lines(name) if x.get("line") != "started")

    def recs(self) -> list[dict[str, Any]]:
        path = self.store / "recommendations.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def table_runs(self) -> list[dict[str, Any]]:
        path = self.store / "agent_runs.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
