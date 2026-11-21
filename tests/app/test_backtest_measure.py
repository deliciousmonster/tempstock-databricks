"""The History caption's measured result is computed from the run's own rows (2026-11-19: dev had
9,030 backtest rows and the page said none was recorded)."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pandas as pd
import pytest
from ws_ui import measure

from tests.conftest import utc
from tests.unit.tables import observed_until, prices_at_close
from weather_signal.config import PRIMARY
from weather_signal.signal import backtest
from weather_signal.signal.model import ThresholdModel

RUN = "h0:threshold_wf:1"
ACTION = {"b": "buy", "s": "sell", "h": "hold"}


def _rows(actions: str, closes: list[float]) -> list[dict]:
    day = date(1991, 1, 10)
    return [
        {
            "target_date": (day + timedelta(days=i)).isoformat(),
            "action": ACTION[a],
            "close": c,
            "backtest_run_id": RUN,
        }
        for i, (a, c) in enumerate(zip(actions, closes, strict=True))
    ]


def test_each_trade_is_judged_by_the_next_close():
    # b: up next (hit); s: flat next (miss); s: down next (hit); h; b: up next (hit); last has no next.
    rows = _rows("bsshbb", [10, 11, 11, 10, 9, 12])
    m = measure.summary(rows)
    assert (m["n_days"], m["n_trades"], m["buys"], m["sells"]) == (5, 4, 2, 2)
    assert m["hit_rate"] == 0.75 and m["always_buy"] == 0.4
    assert (m["first"], m["last"], m["backtest_run_id"]) == ("1991-01-10", "1991-01-15", RUN)


def test_row_order_is_by_date_not_by_arrival():
    rows = _rows("bsshbb", [10, 11, 11, 10, 9, 12])
    assert measure.summary(list(reversed(rows))) == measure.summary(rows)


@pytest.mark.parametrize("rows", [[], _rows("hhh", [1, 2, 3]), [{"key": "backtest"}]])
def test_a_run_with_no_trade_measures_nothing(rows):
    assert measure.summary(rows) is None


def test_the_random_baseline_matches_a_monte_carlo_of_the_same_draw():
    rng = random.Random(20261119)
    n = 600
    closes = [100.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + rng.choice((-0.01, 0.0, 0.01, 0.012))))
    actions = "".join(rng.choice("bbbssh") for _ in range(n))
    m = measure.summary(_rows(actions, closes))
    moves = [b / a - 1 for a, b in zip(closes, closes[1:], strict=False)]
    acts = actions[: n - 1]
    nb, ns = acts.count("b"), acts.count("s")
    sims = []
    for _ in range(20000):
        pick = rng.sample(moves, nb + ns)
        sims.append((sum(x > 0 for x in pick[:nb]) + sum(x < 0 for x in pick[nb:])) / (nb + ns))
    sims.sort()
    mc_mean = sum(sims) / len(sims)
    lo, hi = sims[int(0.025 * len(sims))], sims[int(0.975 * len(sims))]
    assert m["random_mean"] == pytest.approx(mc_mean, abs=0.002)
    assert m["random_band"][0] == pytest.approx(lo, abs=0.006)
    assert m["random_band"][1] == pytest.approx(hi, abs=0.006)
    hit = sum(
        (a == "b" and x > 0) or (a == "s" and x < 0) for a, x in zip(acts, moves, strict=True)
    )
    share = sum(s >= hit / (nb + ns) - 1e-12 for s in sims) / len(sims)
    assert m["share_random_at_or_above"] == pytest.approx(share, abs=0.03)


def test_the_route_serves_the_measured_result_beside_the_rows(client, monkeypatch):
    c, main, _ = client
    rows = _rows("bsshbb", [10, 11, 11, 10, 9, 12])
    monkeypatch.setattr(main.source, "live", lambda key, p: rows if key == "backtest" else [])
    main._backtest.clear()
    data = c.get("/api/backtest").json()["data"]
    assert data["measured"] == measure.summary(rows) and data["measured"]["hit_rate"] == 0.75


def test_each_input_kind_is_measured_apart():
    """The forecast days are the thesis; observed days stand in for a forecast."""
    rows = _rows("bsshbb", [10, 11, 11, 10, 9, 12])
    for r, kind in zip(
        rows, ["forecast", "forecast", "observed", "observed", "observed", "x"], strict=True
    ):
        r["input_kind"] = kind
    m = measure.summary(rows)
    fc, ob = m["by_input"]["forecast"], m["by_input"]["observed"]
    assert (fc["n_days"], fc["n_trades"], fc["hit_rate"]) == (2, 2, 0.5)
    assert (ob["n_days"], ob["n_trades"], ob["hit_rate"]) == (3, 2, 1.0)
    assert m["n_trades"] == 4 and m["hit_rate"] == 0.75


def test_a_kind_with_no_trade_is_null_not_zero():
    rows = _rows("bsshbb", [10, 11, 11, 10, 9, 12])
    for r in rows:
        r["input_kind"] = "observed"
    assert measure.summary(rows)["by_input"]["forecast"] is None


def test_each_kind_keeps_its_trade_count_when_its_slice_is_too_small():
    """One forecast trade on one day gives a null slice but a count of 1."""
    rows = _rows("bsshbb", [10, 11, 11, 10, 9, 12])
    for r, kind in zip(rows, ["forecast", *["observed"] * 5], strict=True):
        r["input_kind"] = kind
    m = measure.summary(rows)
    assert m["by_input"]["forecast"] is None
    assert m["trades_by_input"] == {"forecast": 1, "observed": 3}


def test_sessions_held_on_a_frozen_observation_are_days_not_trades(cal):
    """2026-11-20 dev: ERA5 stopped at 2007-12-27 and every session to 2013 bought on it."""
    days = cal.between(date(2008, 1, 2), date(2008, 1, 31))
    prices = prices_at_close(cal, days[0] - timedelta(days=40), days[-1])
    obs = observed_until(date(2007, 12, 1), date(2007, 12, 27), 3.0)
    inp = backtest.Inputs(pd.DataFrame(columns=["target_date", "lead_days"]), prices, obs)
    model = ThresholdModel(1.0, 0.5, model_name="m", model_version="1")
    rows = backtest.rows(days, inp, cal, model, utc(2026, 11, 20, 12))
    px = prices[prices["ticker"] == PRIMARY].set_index("trade_date")["close"]
    for r in rows:
        r["close"] = float(px[r["target_date"]])
    held = [r["target_date"] for r in rows if r["reasons"] == ["stale_input"]]
    assert held[0] == date(2008, 1, 11) and len(held) == len(days) - 7
    m = measure.summary(rows)
    assert (m["n_days"], m["n_trades"], m["buys"]) == (len(days) - 1, 7, 7)
    assert m["trades_by_input"] == {"forecast": 0, "observed": 7}
    assert m["by_input"]["observed"]["n_days"] == len(days) - 1
