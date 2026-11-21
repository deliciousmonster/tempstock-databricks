"""`dev` Delta, no model: the in-memory reader says `buy` where `signals.signal` says `sell`; the row says `sell`."""

import argparse
from datetime import UTC, datetime

from tests.integration.helpers import frame, scratch_as_of
from weather_signal.agent import hour
from weather_signal.agent.budget import Budget
from weather_signal.agent.proposal import Proposal
from weather_signal.agent.route import Outcome, Turn
from weather_signal.agent.store import SparkStore
from weather_signal.agent.worker import Seams
from weather_signal.jobs.score import write_signal


def test_signal_action_comes_from_signals(spark, n, run_id, tmp_path, monkeypatch):
    as_of = scratch_as_of(f"merge-source-{run_id}")
    rows = [{**r, "action": "sell"} for r in frame(as_of)][:3]
    write_signal(spark, n, rows)
    dates = [r["target_date"].isoformat() for r in rows]

    class LyingReader(SparkStore):
        def inputs(self, _as_of):
            return {d: {"signal_action": "buy", "horizon_days": i} for i, d in enumerate(dates)}

    sessions = []
    from weather_signal.agent import tools

    real_session = tools.Session

    def captured(*a, **kw):  # the dataclass's own __init__ never calls a subclass __post_init__
        sessions.append(real_session(*a, **kw))
        return sessions[-1]

    async def stub_explain(*_a, **_kw):
        s = sessions[-1]
        s.proposals.update(
            {
                d: Proposal(d, False, None, "Stub rationale for the merge source test only.")
                for d in s.dates
            }
        )
        return Outcome("turn", "databricks-gateway", turn=Turn("ok", model="stub"))

    monkeypatch.setattr(tools, "Session", captured)
    monkeypatch.setattr(hour, "explain", stub_explain)
    store = LyingReader(spark, n)
    h = _hour(run_id, as_of, tmp_path)
    hour._explain_and_write(h, store, store.inputs(as_of), h.base_row())
    got = spark.sql(
        f"SELECT signal_action, action FROM {n.table('signals', 'recommendations')} WHERE as_of = :a",
        args={"a": as_of},
    ).collect()
    assert len(got) == 3
    assert {r["signal_action"] for r in got} == {"sell"}
    assert {r["action"] for r in got} <= {"sell", "hold"}


def _hour(run_id, as_of, tmp_path):
    return hour.Hour(
        run_id=f"it{run_id}",
        as_of=as_of,
        started_at=datetime.now(UTC),
        hourly=False,
        args=argparse.Namespace(dry_run=False, eval_run=None, fixture=None),
        seams=Seams(tracking_uri=(tmp_path / "mlruns").as_uri()),
        cache=tmp_path,
        log_path=tmp_path / "runs-eval.jsonl",
        budget=Budget(0.0, clock=lambda: 0.0),
    )
