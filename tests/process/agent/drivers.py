"""Entry points the process tests run in place of `python -m weather_signal.agent`, each with one seam patched.

`python drivers.py <name> run ...`: the patch named first, then the real `worker.main`.
"""

import concurrent.futures
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from weather_signal.agent import hour, tracing, worker


def recv_block():
    """Main thread stuck in a blocking `recv`, a stand-in for a gRPC read."""
    a, b = socket.socketpair()

    def stuck(_store, _h):
        a.recv(1)
        b.close()  # held open by this reference, so recv never sees EOF

    hour.poll = stuck


def regex_block():
    """Main thread inside a regex that holds the GIL for minutes."""

    def stuck(_store, _h):
        re.match(r"(a+)+$", "a" * 40 + "b")

    hour.poll = stuck


def after_token_block():
    """The gateway token is fetched, then the main thread blocks before any request."""
    from crewkit import databricks_gateway

    real = databricks_gateway._token

    async def stuck(gw):
        out = await real(gw)
        time.sleep(3600)
        return out

    databricks_gateway._token = stuck


def stuck_upload():
    """The trace upload leaves a non-daemon thread and an executor task blocked for good."""
    pool = concurrent.futures.ThreadPoolExecutor(1)
    gate = threading.Event()

    def finish(self, attributes, outputs):
        threading.Thread(target=gate.wait, name="stuck-upload", daemon=False).start()
        pool.submit(gate.wait)

    tracing.Tracer.finish = finish


def _is_watchdog(pid):
    import subprocess

    cmd = subprocess.run(
        ["ps", "-o", "command=", "-p", pid], capture_output=True, text=True, check=False
    ).stdout
    return "getppid" in cmd


def child_probe():
    """After each session's context exits, `pgrep -P` this worker and time the session."""
    import json
    import subprocess

    from weather_signal.agent import route

    real = route._session

    async def probed(*a, **kw):
        t0 = time.monotonic()
        try:
            return await real(*a, **kw)
        finally:
            kids = subprocess.run(
                ["pgrep", "-P", str(os.getpid())], capture_output=True, text=True, check=False
            )
            cli = [k for k in kids.stdout.split() if not _is_watchdog(k)]
            with Path(os.environ["CHILD_PROBE_OUT"]).open("a", encoding="utf-8") as f:
                f.write(json.dumps({"children": cli, "session_s": time.monotonic() - t0}) + "\n")

    route._session = probed


def fast_connect():
    """The connect bound at 3 s, so an initialize that never answers fails fast."""
    from weather_signal.agent import route

    route.CONNECT_TIMEOUT_S = 3.0


def env_window():
    """A thread started before `connect` polls two planted names every 1 ms; the tracer's URI is recorded."""
    import json

    out = Path(os.environ["ENV_WINDOW_OUT"])
    os.environ["DATABRICKS_HOST"] = os.environ["ENV_WINDOW_HOST"]
    os.environ["HTTPS_PROXY"] = "http://planted.invalid:3128"
    seen: list[tuple[float, bool]] = []
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            both = "DATABRICKS_HOST" in os.environ and "HTTPS_PROXY" in os.environ
            if not seen or seen[-1][1] != both:
                seen.append((time.monotonic(), both))
            time.sleep(0.001)

    th = threading.Thread(target=poll, name="env-poller", daemon=True)
    th.start()
    real_end, real_init = hour._end, tracing.Tracer.__init__

    def end(h, store, row):
        stop.set()
        th.join(1)
        seen.append(
            (time.monotonic(), "DATABRICKS_HOST" in os.environ and "HTTPS_PROXY" in os.environ)
        )
        out.write_text(json.dumps({"seen": seen, "uri": uris}), encoding="utf-8")
        return real_end(h, store, row)

    uris: list[str] = []

    def tracer_init(self, uri, **kw):
        uris.append(uri)
        real_init(self, os.environ["ENV_WINDOW_MLFLOW"], **kw)

    hour._end = end
    tracing.Tracer.__init__ = tracer_init


if __name__ == "__main__":
    for name in sys.argv[1].split(","):
        globals()[name]()
    worker.main(sys.argv[3:])
