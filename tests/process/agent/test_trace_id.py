"""The run row's `trace_id` is MLflow's trace id, which `get_trace` resolves; a span id doesn't."""

import re

from tests.process.agent.harness import World


def test_run_row_trace_id_resolves(world: World, monkeypatch):
    import mlflow

    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")  # the worker writes to a local file store

    assert world.run().rc == 0
    row = world.run_row()
    assert re.fullmatch(r"tr-[0-9a-f]{32}", row["trace_id"]), row["trace_id"]
    assert {r["trace_id"] for r in world.recs()} == {row["trace_id"]}
    mlflow.set_tracking_uri(world.seams["tracking_uri"])
    trace = mlflow.get_trace(row["trace_id"])
    assert trace is not None and trace.info.trace_id == row["trace_id"]


def test_mlflow_log_lands_under_the_cache(world: World):
    """`mlflow.anthropic.autolog` writes `.claude/mlflow/` under the cwd; the worker's cwd is its cache."""
    assert world.run().rc == 0
    assert not (world.root / ".claude").exists()
    assert (world.cache / ".claude" / "mlflow").is_dir()
