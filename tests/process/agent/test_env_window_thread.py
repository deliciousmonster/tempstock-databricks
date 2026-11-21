"""crewkit's reduced `os.environ` is process-wide for the length of `connect`: measured, bounded, logged."""

import json

from weather_signal.agent import hour
from weather_signal.agent.route import CONNECT_TIMEOUT_S


def test_window_seen_by_a_thread(world):
    out = world.root / "window.json"
    world.seams.pop("tracking_uri")
    env = {
        "ENV_WINDOW_OUT": str(out),
        "ENV_WINDOW_HOST": world.gw.host,
        "ENV_WINDOW_MLFLOW": (world.root / "mlruns").as_uri(),
    }
    assert world.run(driver="env_window", env=env).rc == 0
    d = json.loads(out.read_text())
    seen = d["seen"]
    assert seen[0][1] is True
    absent = [i for i, (_, both) in enumerate(seen) if not both]
    assert absent, "the thread never saw the reduced environment"
    first, back = absent[0], next(i for i in range(absent[0], len(seen)) if seen[i][1])
    assert seen[back][0] - seen[first][0] < CONNECT_TIMEOUT_S
    assert seen[-1][1] is True
    assert d["uri"] == ["databricks://gw"]
    log = world.log.read_text()
    assert (
        "env window opens; threads=" in log
        and "env-poller" in log.split("env window opens; threads=")[1].splitlines()[0]
    )


def test_connect_builder_gets_the_profile(monkeypatch):
    calls = []

    class Builder:
        def profile(self, p):
            calls.append(("profile", p))
            return self

        def serverless(self):
            calls.append(("serverless",))
            return self

        def getOrCreate(self):
            return "session"

    import sys
    import types

    mod = types.ModuleType("databricks.connect")
    mod.DatabricksSession = types.SimpleNamespace(builder=Builder())
    monkeypatch.setitem(sys.modules, "databricks.connect", mod)
    assert hour.connect_spark("weather-signal-agent") == "session"
    assert calls[0] == ("profile", "weather-signal-agent")
