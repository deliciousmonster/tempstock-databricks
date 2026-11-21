"""A benchmark that had not listed by the decision's fifth session back is no stale input; once
it has listed, a missing return holds as `stale_prices` again."""

from datetime import date, timedelta

import pytest

from tests.unit import tables
from weather_signal import asof
from weather_signal.config import BENCHMARK_FIRST
from weather_signal.signal.features import Tables, build
from weather_signal.signal.model import SignalInput, freshness


def _reasons(cal, day: date, drop: set[str]) -> tuple[dict, tuple[str, ...]]:
    prices = tables.prices_at_close(cal, day - timedelta(days=30), day + timedelta(days=3))
    tabs = Tables(
        tables.forecast(day - timedelta(days=10), 20, 30), prices[~prices["ticker"].isin(drop)]
    )
    as_of = asof.backtest_as_of(day)
    f = build(day, asof.target_date(day, 0), tabs, as_of=as_of, cal=cal).values
    return f, freshness(SignalInput(as_of, day, 0, f, as_of))


def test_before_either_benchmark_listed_nothing_is_stale_prices(cal):
    f, reasons = _reasons(cal, date(1992, 6, 1), {"SPY", "XLP"})
    assert (f["spy_unlisted"], f["xlp_unlisted"]) == (1.0, 1.0)
    assert "stale_prices" not in reasons


@pytest.mark.parametrize("day", [date(1995, 6, 1), date(2015, 3, 10)])
def test_a_listed_benchmark_missing_its_return_is_stale_prices(cal, day):
    f, reasons = _reasons(cal, day, {"SPY"})
    assert f["spy_unlisted"] == 0.0 and "stale_prices" in reasons


def test_unlisted_turns_off_once_the_previous_session_has_a_five_day_return(cal):
    first = BENCHMARK_FIRST["XLP"]
    s = cal.between(first, first + timedelta(days=20))
    # Deciding on s[6], the previous session s[5] is five sessions after the first.
    assert _reasons(cal, s[5], set())[0]["xlp_unlisted"] == 1.0
    assert _reasons(cal, s[6], set())[0]["xlp_unlisted"] == 0.0
