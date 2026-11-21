"""The train job: the walk's latest refit is registered with its gate; `@champion` only on a pass."""

import json
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from tests.unit.test_signal_backtest import _walk_world
from weather_signal import asof, config
from weather_signal.sessions import Calendar, Session
from weather_signal.signal import backtest, gate, train
from weather_signal.signal.model import ThresholdModel

NOW = datetime(2015, 3, 2, 12, tzinfo=UTC)


class Reg:
    def __init__(self, newest=None, disagree=False):
        self.newest, self.disagree = newest, disagree
        self.registered, self.aliased, self.dropped = [], [], 0

    def latest(self, name):
        return self.newest

    def register(self, name, model, tags):
        self.registered.append(dict(tags))
        return str(len(self.registered))

    def predict(self, name, version, values):
        p = (
            json.loads(self.registered[-1]["params"])
            if self.registered
            else json.loads(self.newest[1]["params"])
        )
        out = train.actions(ThresholdModel(p["coef"], p["threshold"]), values)
        return ["hold" for _ in out] if self.disagree else out

    def set_alias(self, name, version):
        self.aliased.append(version)

    def drop_alias(self, name):
        self.dropped += 1


@pytest.fixture
def world(cal):
    """2014 rises 1 % a session on a +2 anomaly; 2015 falls 1 % a session on the same anomaly."""
    return _walk_world(cal, 0.01)[1]


@pytest.fixture
def passing(monkeypatch):
    real = gate.judge

    def judged(days, **kw):
        g = real(days, **kw)
        return gate.Gate(0.5, 0.1, True, g.n, g.trades)

    monkeypatch.setattr(backtest.gate, "judge", judged)


def test_the_deployed_model_is_the_walk_s_latest_refit(world, cal):
    m, tags = train.deployed(world, cal, NOW)
    first_2015 = cal.between(date(2015, 1, 1), date(2015, 1, 9))[0]
    assert tags["fit_as_of"] == train.as_of_tag(asof.backtest_as_of(first_2015))
    assert tags["period_through"] == "2015-12-31"
    assert (m.coef, m.threshold) == (1.0, 0.5)
    assert (tags["coef"], tags["threshold"]) == ("1.0", "0.5")
    assert tags["fit"] == "threshold_wf v2"
    # The 2015 rows trade the same refit the tags name.
    rows = backtest.rows(backtest.sessions(world.prices, NOW), world, cal, None, NOW)
    last = json.loads(rows[-1]["features"])
    assert (last["model_coef"], last["model_threshold_c"]) == (m.coef, m.threshold)


def test_n_train_counts_the_labels_known_at_the_refit(world, cal):
    _, tags = train.deployed(world, cal, NOW)
    days_2014 = [d for d in backtest.sessions(world.prices, NOW) if d.year == 2014]
    # Each 2014 session's label closes two sessions later; the last two are known only in 2015.
    assert int(tags["n_train"]) == len(days_2014) - 2


def test_the_tags_carry_the_gate_and_the_estimate_basis(world, cal):
    _, tags = train.deployed(world, cal, NOW)
    assert tags["gate_pass"] == "false"  # buying 2015's 1 % falls loses
    assert float(tags["gate_total"]) < 0 and int(tags["gate_n"]) > 0
    basis = json.loads(tags["est_basis"])
    assert basis["fit_as_of"] == tags["fit_as_of"] and basis["cost"] == 0.001
    assert basis["buy"]["mean_gross"] == pytest.approx(-0.01) and basis["sell"]["n"] == 0


def test_a_failing_gate_registers_and_removes_the_alias(world, cal):
    reg = Reg()
    assert train.train(world, cal, reg, "n", NOW) == "1, no champion: the gate failed"
    assert reg.registered[0]["gate_pass"] == "false"
    assert reg.aliased == [] and reg.dropped == 1


@pytest.mark.usefixtures("passing")
def test_a_passing_gate_sets_the_alias(world, cal):
    reg = Reg()
    assert train.train(world, cal, reg, "n", NOW) == "1, champion"
    assert reg.registered[0]["gate_pass"] == "true" and reg.aliased == ["1"]
    assert reg.dropped == 0


@pytest.mark.usefixtures("passing")
def test_an_equal_newest_version_is_reused(world, cal):
    _, tags = train.deployed(world, cal, NOW)
    reg = Reg(newest=("4", {**tags, "mlflow_run_id": "x"}))
    assert train.train(world, cal, reg, "n", NOW) == "4, champion"
    assert reg.registered == [] and reg.aliased == ["4"]


@pytest.mark.usefixtures("passing")
def test_a_newest_version_of_another_fit_is_not_reused(world, cal):
    _, tags = train.deployed(world, cal, NOW)
    reg = Reg(newest=("4", {**tags, "fit_as_of": "2014-01-02T21:30:00Z"}))
    assert train.train(world, cal, reg, "n", NOW) == "1, champion"


@pytest.mark.usefixtures("passing")
def test_a_pyfunc_that_disagrees_with_its_tags_gets_no_alias(world, cal):
    reg = Reg(disagree=True)
    with pytest.raises(train.TrainRefused):
        train.train(world, cal, reg, "n", NOW)
    assert reg.aliased == []


def cal_of(days):
    return Calendar(
        [
            Session(
                d,
                datetime(d.year, d.month, d.day, 14, 30, tzinfo=UTC),
                datetime(d.year, d.month, d.day, 21, tzinfo=UTC),
            )
            for d in days
        ],
        days[-1],
    )


DAYS = [date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9)]
AS_OF = datetime(2026, 11, 10, tzinfo=UTC)


def closes_to(day):
    return pd.DataFrame({"ticker": [config.PRIMARY], "trade_date": [pd.Timestamp(day)]})


def test_closes_short_of_the_instant_refuse_the_fit():
    with pytest.raises(train.TrainRefused, match="needs 2026-11-06"):
        train.require_complete(closes_to("2026-11-05"), cal_of(DAYS), AS_OF)


def test_the_label_lag_sets_the_last_session_needed():
    train.require_complete(closes_to("2026-11-06"), cal_of(DAYS), AS_OF)
    with pytest.raises(train.TrainRefused):
        train.require_complete(closes_to("2026-11-06"), cal_of(DAYS), AS_OF + timedelta(days=2))


def test_the_pyfunc_applies_the_same_rule():
    py = train.Pyfunc(0.5, 0.1)
    x = pd.DataFrame({"forecast_anomaly_c": [-3.0, 0.0, 3.0]})
    assert py.predict(None, x) == train.actions(ThresholdModel(0.5, 0.1), [-3.0, 0.0, 3.0])
