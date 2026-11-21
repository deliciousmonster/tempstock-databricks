"""`@champion`: missing holds with `no_champion`, present trades only inside its period."""

import json
import types
from datetime import date

import pytest

from tests.conftest import utc
from tests.unit.test_features_parity import _tabs
from weather_signal import config
from weather_signal.jobs import score as score_job
from weather_signal.signal import score
from weather_signal.signal.champion import Deployed, from_tags
from weather_signal.signal.estimate import Basis
from weather_signal.signal.gate import Side
from weather_signal.signal.model import AlwaysHold, Signal, SignalInput

N = config.Names("workspace", "dev_")


class RegistryError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.error_code = code


class Client:
    def __init__(self, err: Exception) -> None:
        self.err = err

    def get_model_version_by_alias(self, name, alias):
        raise self.err


def test_missing_alias_scores_always_hold():
    # Measured on dev: both a missing alias and a missing model answer RESOURCE_DOES_NOT_EXIST.
    m = score_job.champion(N, Client(RegistryError("RESOURCE_DOES_NOT_EXIST")))
    assert isinstance(m, AlwaysHold) and m.model_name == "always_hold"


@pytest.mark.parametrize(
    "err", [ConnectionError("reset"), RegistryError("TEMPORARILY_UNAVAILABLE")]
)
def test_an_unreadable_registry_fails_the_run(err):
    with pytest.raises(score_job.TaskFailed, match="no_champion"):
        score_job.champion(N, Client(err))


def test_score_task_writes_nothing_when_the_registry_is_unreadable(monkeypatch):
    wrote = []
    monkeypatch.setattr(score_job.check, "may_write", lambda *a: True)
    monkeypatch.setattr(score_job, "load_tables", lambda *a: None)
    monkeypatch.setattr(score_job, "load_calendar", lambda: None)
    monkeypatch.setattr(score_job, "write_signal", lambda *a: wrote.append(a))

    real = score_job.champion

    def unreadable(n):
        return real(n, Client(ConnectionError("reset")))

    monkeypatch.setattr(score_job, "champion", unreadable)

    class Cat:
        def guard_rows(self):
            return []

    with pytest.raises(score_job.TaskFailed):
        score_job.score_task(None, Cat(), N, None, 30, None)
    assert wrote == []


def test_champion_reads_the_neutral_model_name():
    asked = []

    class Recording:
        def get_model_version_by_alias(self, name, alias):
            asked.append((name, alias))
            raise RegistryError("RESOURCE_DOES_NOT_EXIST")

    score_job.champion(N, Recording())
    assert asked == [("workspace.dev_signals.stock_signal", "champion")]


FIT = "2026-01-02T21:30:00Z"
BASIS = Basis(FIT, {"buy": Side(0.003, 40), "sell": Side(-0.004, 40)})


def _tags(**over):
    t = {
        "params": '{"coef":1.0,"kind":"threshold","threshold":0.5}',
        "fit_as_of": FIT,
        "period_through": "2026-12-03",
        "est_basis": BASIS.to_json(),
        "gate_pass": "true",
    }
    return {k: v for k, v in {**t, **over}.items() if v is not None}


class Found:
    def __init__(self, tags):
        self.mv = types.SimpleNamespace(tags=tags, version=9)

    def get_model_version_by_alias(self, name, alias):
        return self.mv


def _frame(cal, model):
    return score.frame(utc(2026, 12, 2, 12, 30), 30, _tabs(cal, 30), cal, model)


def test_no_alias_holds_every_target_with_no_champion(cal):
    m = score_job.champion(N, Client(RegistryError("RESOURCE_DOES_NOT_EXIST")))
    rows = [r for r in _frame(cal, m) if r["horizon_days"] < 6]
    assert rows and {r["action"] for r in rows} == {"hold"}
    assert all(r["reasons"][-1:] == ["no_champion"] for r in rows)
    assert {r["est_reason"] for r in rows} == {"no_champion"}


def test_the_champion_trades_only_targets_inside_its_period(cal):
    m = score_job.champion(N, Found(_tags()))
    assert isinstance(m, Deployed) and m.period_through == date(2026, 12, 3)
    by_target = {r["target_date"]: r for r in _frame(cal, m)}
    inside, outside = by_target[date(2026, 12, 3)], by_target[date(2026, 12, 4)]
    assert inside["action"] == "buy" and inside["model_version"] == "9"
    assert (outside["action"], outside["reasons"]) == ("hold", ["no_champion"])
    assert outside["est_reason"] == "no_champion" and outside["est_price"] is None


def test_a_buy_inside_the_period_carries_its_estimate(cal):
    m = score_job.champion(N, Found(_tags()))
    r = next(r for r in _frame(cal, m) if r["target_date"] == date(2026, 12, 3))
    basis = json.loads(r["est_basis"])
    assert r["est_reason"] is None and basis["fit_as_of"] == FIT and basis["side"] == "buy"
    # Close of Tue 1st; Wed 2nd and Thu 3rd are the sessions to the target.
    assert (basis["close_date"], basis["sessions"]) == ("2026-12-01", 2)
    assert r["est_price"] == pytest.approx(basis["close"] * 1.002**2, abs=1e-4)


def test_a_version_without_a_period_never_trades(cal):
    m = score_job.champion(N, Found(_tags(period_through=None)))
    rows = [r for r in _frame(cal, m) if r["horizon_days"] < 6]
    assert {r["action"] for r in rows} == {"hold"}
    assert {r["est_reason"] for r in rows} == {"no_champion"}


def test_a_basis_from_another_fit_gives_no_estimate(cal):
    other = Basis("2025-01-02T21:30:00Z", BASIS.sides).to_json()
    m = score_job.champion(N, Found(_tags(est_basis=other)))
    r = next(r for r in _frame(cal, m) if r["target_date"] == date(2026, 12, 3))
    assert r["action"] == "buy" and (r["est_price"], r["est_reason"]) == (None, "no_basis")


def test_the_champion_wrapper_holds_past_the_period():
    d = from_tags(_tags(period_through="2026-12-31"), "n", "1")
    x = SignalInput(utc(2026, 12, 2, 12, 30), date(2027, 1, 1), 0, {}, utc(2026, 12, 2))
    assert d.predict(x) == Signal("hold", 0.0, 0.0, ("no_champion",))
