"""est_price: the last visible close compounded per session by the side's mean net of costs."""

from __future__ import annotations

import json
from datetime import date

import pytest

from weather_signal.signal import estimate as est
from weather_signal.signal.gate import Side

FIT = "2026-01-02T21:30:00Z"
CLOSE = (100.0, date(2026, 11, 19))  # a Thursday
MONDAY = date(2026, 11, 23)


def _basis(buy=(0.003, 40), sell=(-0.004, 35), fit=FIT):
    return est.Basis(fit, {"buy": Side(*buy), "sell": Side(*sell)})


def _est(cal, action="buy", *, fit=FIT, basis=None, close=CLOSE, target=MONDAY):
    b = _basis() if basis is None else basis
    return est.estimate(action, fit_as_of=fit, basis=b, close=close, target=target, cal=cal)


def test_a_buy_by_hand(cal):
    # Sessions after Thu 19th through Mon 23rd: Fri 20th and Mon 23rd.
    # move = 0.003 - 0.001 = 0.002; 100 * 1.002 ** 2 = 100.4004.
    e = _est(cal)
    assert e.price == pytest.approx(100.4004) and e.reason is None
    assert e.basis == {
        "close": 100.0,
        "close_date": "2026-11-19",
        "side": "buy",
        "sessions": 2,
        "mean_gross": 0.003,
        "cost": 0.001,
        "move": pytest.approx(0.002),
        "n_trades": 40,
        "fit_as_of": FIT,
    }


def test_the_move_is_rounded_to_8_dp_as_the_reference_rounds_it(cal):
    # Unrounded, 100 * 1.002000745523 ** 2 = 100.4005; the reference's 1.00200075 ** 2 gives 100.4006.
    e = _est(cal, basis=_basis(buy=(0.003000745523, 40)))
    assert e.basis["move"] == 0.00200075 and e.price == 100.4006


def test_a_sell_sits_below_the_close_by_hand(cal):
    # move = -0.004 + 0.001 = -0.003; 100 * 0.997 ** 2 = 99.4009.
    e = _est(cal, "sell")
    assert e.price == pytest.approx(99.4009) and e.basis["move"] == pytest.approx(-0.003)


def test_a_weekend_target_counts_sessions_not_days(cal):
    # Sat 21st: Fri 20th is the one session after the close.
    e = _est(cal, target=date(2026, 11, 21))
    assert e.basis["sessions"] == 1 and e.price == pytest.approx(100.2)


def test_the_columns_carry_the_basis_as_json(cal):
    cols = _est(cal).columns()
    assert list(cols) == ["est_price", "est_reason", "est_basis"]
    assert json.loads(cols["est_basis"])["side"] == "buy"
    assert est.Estimate(None, est.HOLD).columns() == {
        "est_price": None,
        "est_reason": "hold",
        "est_basis": None,
    }


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"fit": None}, "no_champion"),
        ({"action": "hold"}, "hold"),
        ({"basis": _basis(fit="2025-01-02T21:30:00Z")}, "no_basis"),
        ({"basis": _basis(buy=(0.003, 29))}, "few_trades"),
        ({"basis": _basis(buy=(None, 0))}, "few_trades"),
        ({"basis": _basis(buy=(0.001, 40))}, "no_edge_after_costs"),
        ({"basis": _basis(buy=(-0.002, 40))}, "no_edge_after_costs"),
        ({"action": "sell", "basis": _basis(sell=(-0.001, 40))}, "no_edge_after_costs"),
        ({"action": "sell", "basis": _basis(sell=(0.002, 40))}, "no_edge_after_costs"),
        ({"close": None}, "no_close"),
    ],
)
def test_each_reason_gives_no_price(cal, kwargs, reason):
    e = _est(cal, **kwargs)
    assert (e.price, e.reason, e.basis) == (None, reason, {})


def test_no_champion_wins_over_hold(cal):
    assert _est(cal, "hold", fit=None).reason == "no_champion"


def test_thirty_trades_is_enough(cal):
    assert _est(cal, basis=_basis(buy=(0.003, 30))).reason is None


def test_the_basis_round_trips_through_its_tag():
    b = _basis()
    assert est.Basis.from_json(b.to_json()) == b
    assert json.loads(b.to_json())["buy"] == {"mean_gross": 0.003, "n": 40}
