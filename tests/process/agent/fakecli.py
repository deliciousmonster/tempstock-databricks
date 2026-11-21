"""A stand-in for the Claude Code CLI 2.1.326 that plays one hour's turn through the in-process tools.

Its cue is `cue.json` beside the launcher (crewkit's reduced environment can't carry one); each spawn
takes the next entry of `spawns`. It logs every frame and its environment's names to `fake.log`.
"""

import hashlib
import json
import os
import queue
import re
import signal
import sys
import threading
import time
from pathlib import Path

HERE = Path(os.environ["FAKE_DIR"])
ACCOUNTS = Path(__file__).resolve().parents[2] / "fixtures" / "agent" / "accounts"
VERSION = "2.1.326"
RETRY_AT = (1, 2, 3, 5, 10, 19, 37, 60, 90, 120, 150)


def log(entry):
    with (HERE / "fake.log").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def cue():
    path = HERE / "cue.json"
    spawns = json.loads(path.read_text(encoding="utf-8")).get("spawns", []) if path.exists() else []
    n = (
        sum(
            1
            for line in (HERE / "fake.log").read_text(encoding="utf-8").splitlines()
            if '"spawn"' in line
        )
        if (HERE / "fake.log").exists()
        else 0
    )
    return n, (spawns[n] if n < len(spawns) else {})


def send(frame):
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


class Cli:
    def __init__(self, spawn, c):
        self.spawn, self.c, self.next_id, self.hooks = spawn, c, 0, []
        self.dates, self.inputs = [], {}

    def read(self, timeout=None):
        try:
            line = LINES.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is None:
            if self.c.get("eof_deaf"):
                time.sleep(self.c.get("eof_deaf"))
            os._exit(0)
        frame = json.loads(line)
        log(
            {
                "spawn_n": self.spawn,
                "frame": frame.get("type"),
                "subtype": (frame.get("request") or {}).get("subtype"),
            }
        )
        return frame

    def wait(self, seconds):
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            frame = self.read(timeout=left)
            if frame is not None and frame.get("type") == "control_request":
                self.control(frame)

    def answer(self, frame, response):
        send(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": frame["request_id"],
                    "response": response,
                },
            }
        )

    def request(self, req):
        self.next_id += 1
        rid = f"fake_{self.next_id}"
        send({"type": "control_request", "request_id": rid, "request": req})
        while True:
            frame = self.read()
            if (
                frame.get("type") == "control_response"
                and frame["response"].get("request_id") == rid
            ):
                return frame["response"].get("response") or {}
            if frame.get("type") == "control_request":
                self.control(frame)

    def control(self, frame):
        sub = frame["request"].get("subtype")
        if sub == "interrupt":
            self.answer(frame, {})
            raise Interrupted
        self.answer(frame, {})

    def mcp(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            msg["id"] = self.next_id + 1000
        out = self.request({"subtype": "mcp_message", "server_name": "ws", "message": msg})
        return out.get("mcp_response", {})

    def call(self, name, args):
        res = self.mcp("tools/call", {"name": name, "arguments": args}).get("result", {})
        text = (res.get("content") or [{}])[0].get("text", "{}")
        log({"tool": name, "args": args, "is_error": bool(res.get("isError"))})
        return json.loads(text)


class Interrupted(Exception):
    pass


LINES = queue.Queue()


def pump():
    for line in sys.stdin:
        LINES.put(line)
    LINES.put(None)


def initialize_answer(c):
    account = json.loads(
        (ACCOUNTS / f"{c.get('account', 'subscription')}.json").read_text(encoding="utf-8")
    )
    return {
        "claude_code_version": VERSION,
        "hook_on_failure_supported": True,
        "commands": [],
        "agents": [],
        "output_style": "default",
        "available_output_styles": [],
        "models": [],
        "pid": os.getpid(),
        "account": account,
    }


def init_frame(c):
    send(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "fake",
            "model": "claude-haiku-4-5",
            "claude_code_version": VERSION,
            "apiKeySource": c.get("api_key_source", "none"),
            "tools": [
                "mcp__ws__get_inputs",
                "mcp__ws__get_prices",
                "mcp__ws__submit_recommendation",
            ],
            "mcp_servers": [{"name": "ws", "status": "connected"}],
        }
    )


def probe_hooks(cli, c):
    for name in c.get("hook_probe", []):
        for hid in cli.hooks:
            out = cli.request(
                {
                    "subtype": "hook_callback",
                    "callback_id": hid,
                    "input": {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": {}},
                }
            )
            log(
                {
                    "hook": name,
                    "decision": (out.get("hookSpecificOutput") or {}).get(
                        "permissionDecision", "allow"
                    ),
                }
            )


def turn(cli, user_text):
    c = cli.c
    cli.dates = re.findall(r"\d{4}-\d{2}-\d{2}(?=,|$)", user_text.split("target_dates:")[-1])
    init_frame(c)
    probe_hooks(cli, c)
    mode = c.get("turn", "submit")
    if mode == "hang":
        cli.wait(3600)
    if mode == "retry_schedule":
        start = time.monotonic()
        for at in RETRY_AT:
            cli.wait(max(0, at - (time.monotonic() - start)))
            log({"retry_at": at})
        return result(c, error_status=503)
    if mode == "error_status":
        return result(c, error_status=c["status"])
    cli.mcp(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "fake", "version": "0"},
        },
    )
    cli.mcp("notifications/initialized", notify=True)
    submit(cli, c)
    return result(c)


def submit(cli, c):
    cli.call("get_prices", {"days": 5})
    limit = c.get("submit_n", len(cli.dates))
    for i, d in enumerate(cli.dates):
        if i == c.get("kill_after", -1):
            os.kill(os.getpid(), signal.SIGKILL)
        row = cli.call("get_inputs", {"target_date": d})
        if i >= limit:
            continue
        reason = c.get("downgrade", {}).get(d)
        text = f"Signal {row.get('signal_action')} at score {row.get('score')}; anomaly {row.get('anomaly_c')} C, 5-day return {row.get('ret_5d')}."
        args = {
            "target_date": d,
            "signal_action": row.get("signal_action"),
            "horizon_days": row.get("horizon_days"),
            "downgrade": bool(reason),
            "rationale": text,
        }
        if reason:
            args["reason"] = reason
        cli.call("submit_recommendation", args)


def result(c, error_status=None):
    err = error_status is not None
    send(
        {
            "type": "result",
            "subtype": "success",
            "duration_ms": 1200,
            "duration_api_ms": 900,
            "is_error": err,
            "num_turns": 4,
            "session_id": "fake",
            "total_cost_usd": 0.0007,
            "result": f"API Error: {error_status}" if err else "done",
            "api_error_status": error_status,
        }
    )


def main():
    n, c = cue()
    headers = os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "")
    log(
        {
            "spawn": n,
            "pid": os.getpid(),
            "pgid": os.getpgid(0),
            "env_names": sorted(os.environ),
            "headers_sha": hashlib.sha256(headers.encode()).hexdigest() if headers else None,
        }
    )
    if c.get("eof_deaf"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    cli = Cli(n, c)
    threading.Thread(target=pump, daemon=True).start()
    while True:
        frame = cli.read()
        if frame.get("type") == "control_request":
            req = frame["request"]
            if req.get("subtype") == "initialize":
                if c.get("init") == "hang":
                    continue
                cli.hooks = [
                    i
                    for m in (req.get("hooks") or {}).get("PreToolUse", [])
                    for i in m.get("hookCallbackIds", [])
                ]
                cli.answer(frame, initialize_answer(c))
                if c.get("after_init") == "kill":
                    os.kill(os.getpid(), signal.SIGKILL)
                continue
            cli.answer(frame, {})
        elif frame.get("type") == "user":
            try:
                turn(cli, frame["message"]["content"])
            except Interrupted:
                log({"interrupted": True})


if __name__ == "__main__":
    main()
