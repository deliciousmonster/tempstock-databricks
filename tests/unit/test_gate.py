"""The champion gate: net P&L at 0.001 a position day against 200 year-block shuffles, seed 0."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from weather_signal.signal import gate
from weather_signal.signal.gate import Day


def _series(edge: float, years: int = 10, per_year: int = 120, seed: int = 7) -> list[Day]:
    """Daily returns of sd 1 %; the position calls the sign right with probability `edge`."""
    rng = random.Random(seed)
    out = []
    for y in range(years):
        for i in range(per_year):
            ret = rng.gauss(0.0, 0.01)
            right = rng.random() < edge
            pos = (1 if ret > 0 else -1) * (1 if right else -1)
            out.append(Day(date(2000 + y, 1, 3) + timedelta(days=i), pos, ret))
    return out


def test_the_constants_are_the_owner_s():
    assert (gate.COST, gate.SHUFFLES, gate.SEED, gate.PERCENTILE) == (0.001, 200, 0, 95.0)


def test_daily_pnl_charges_the_cost_only_on_a_position():
    assert gate.pnl(1, 0.02) == pytest.approx(0.019)
    assert gate.pnl(-1, 0.02) == pytest.approx(-0.021)
    assert gate.pnl(0, 0.02) == 0.0


def test_a_planted_edge_passes():
    g = gate.judge(_series(0.7))
    assert g.passed and g.total > 0 and g.p95 is not None and g.total > g.p95
    assert (g.n, g.trades) == (1200, 1200)


def test_noise_fails():
    g = gate.judge(_series(0.5))
    assert not g.passed
    assert g.p95 is not None and g.total <= max(g.p95, 0.0)


def test_noise_fails_on_the_shuffles_even_without_costs():
    days = _series(0.5, seed=11)
    g = gate.judge(days, cost=0.0)
    assert not g.passed and g.p95 is not None and g.total <= g.p95


def test_the_shuffles_are_deterministic_by_seed():
    days = _series(0.55)
    assert gate.judge(days) == gate.judge(days)
    assert gate.shuffle_totals(days) == gate.shuffle_totals(days, seed=0)
    assert gate.shuffle_totals(days, seed=1) != gate.shuffle_totals(days)
    assert len(gate.shuffle_totals(days)) == 200


def test_shuffles_move_whole_years_and_pay_the_same_costs():
    # Two one-day years: the only other order puts the buy on the other year's return.
    days = [Day(date(2000, 6, 1), 1, 0.05), Day(date(2001, 6, 1), 0, -0.02)]
    totals = set(round(t, 10) for t in gate.shuffle_totals(days))
    assert totals <= {round(0.05 - 0.001, 10), round(-0.02 - 0.001, 10)}
    assert len(totals) == 2


def test_beating_the_shuffles_is_not_enough_below_zero():
    """30 one-day years: the one buy sits on the only small loss; any other year loses 5 %."""
    days = [
        Day(date(1990 + y, 6, 1), 1 if y == 0 else 0, -0.0005 if y == 0 else -0.05)
        for y in range(30)
    ]
    g = gate.judge(days)
    assert g.p95 is not None and g.total > g.p95
    assert g.total < 0 and not g.passed


def test_no_days_never_passes():
    g = gate.judge([])
    assert not g.passed and (g.total, g.n, g.trades) == (0, 0, 0)


def test_the_percentile_interpolates_between_ranks():
    assert gate.percentile([0.0, 10.0], 95) == pytest.approx(9.5)
    assert gate.percentile(list(range(101)), 95) == 95


def test_sides_average_the_gross_return_held_on_each_side():
    days = [
        Day(date(2000, 1, d), p, r)
        for d, p, r in ((3, 1, 0.01), (4, 1, 0.03), (5, -1, -0.02), (6, 0, 0.5))
    ]
    s = gate.sides(days)
    assert s["buy"] == gate.Side(pytest.approx(0.02), 2)
    assert s["sell"] == gate.Side(pytest.approx(-0.02), 1)


def test_a_sides_mean_is_rounded_to_8_dp():
    days = [Day(date(2000, 1, d), 1, r) for d, r in ((3, 0.01), (4, 0.01), (5, 0.0100001))]
    assert gate.sides(days)["buy"].mean_gross == 0.01000003
