"""Re-record the frame fixtures: the bundled CLI against a local server answering 503, zero prompts spent.

    uv run python tests/fixtures/agent/frames/record.py
Each route's init and result frames land beside this file with ids, paths and the port redacted.
"""

import json
import os
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anyio
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

HERE = Path(__file__).parent
ROUTES = {
    "bearer": {"ANTHROPIC_AUTH_TOKEN": "scratch-not-a-token"},
    "api_key": {"ANTHROPIC_API_KEY": "sk-ant-scratch-not-a-key"},
    "bedrock": {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
        "AWS_REGION": "us-east-1",
    },
}


class H(BaseHTTPRequestHandler):
    def _r(self):
        n = int(self.headers.get("content-length") or 0)
        if n:
            self.rfile.read(n)
        body = json.dumps(
            {"type": "error", "error": {"type": "api_error", "message": "fixture"}}
        ).encode()
        self.send_response(503)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _r

    def log_message(self, *_a):
        return


def redact(frame, root, port):
    text = json.dumps(frame).replace(str(root), "<tmp>").replace(f"127.0.0.1:{port}", "<gateway>")
    text = re.sub(r'"(session_id|uuid)": "[^"]+"', r'"\1": "<\1>"', text)
    return json.loads(text)


async def record(name, extra, port, root):
    home = root / name
    (home / "cfg").mkdir(parents=True)
    base = {
        "ANTHROPIC_BEDROCK_BASE_URL"
        if name == "bedrock"
        else "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}"
    }
    env = {
        **base,
        **extra,
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / "cfg"),
        "CLAUDE_CODE_MAX_RETRIES": "0",
    }
    opts = ClaudeAgentOptions(
        model="haiku",
        tools=[],
        setting_sources=[],
        strict_mcp_config=True,
        permission_mode="dontAsk",
        env=env,
        cwd=str(home),
    )
    frames = await collect(opts)
    write(name, frames, root, port)


async def collect(opts):
    frames = []
    async with ClaudeSDKClient(opts) as c:
        await c.query("say hi")
        with anyio.fail_after(60):
            async for m in c.receive_messages():
                frames.append(m)
                if type(m).__name__ == "ResultMessage":
                    break
    return frames


def write(name, frames, root, port):
    out = {}
    for m in frames:
        kind = type(m).__name__
        if kind == "SystemMessage" and m.subtype == "init":
            out["init"] = m.data
        elif kind == "ResultMessage":
            out["result"] = {k: v for k, v in vars(m).items()}
    (HERE / f"{name}.json").write_text(
        json.dumps(redact(out, root, port), indent=2, default=str) + "\n", encoding="utf-8"
    )


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    for k in [k for k in os.environ if k.startswith(("ANTHROPIC_", "CLAUDE_", "AWS_"))]:
        os.environ.pop(k)
    with tempfile.TemporaryDirectory() as tmp:
        for name, extra in ROUTES.items():
            anyio.run(record, name, extra, port, Path(tmp))
            print(name, "recorded", file=sys.stderr)
    srv.shutdown()


if __name__ == "__main__":
    main()
