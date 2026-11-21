"""One `:30` grid live; 21:30 UTC on sessions in the backtest, after every close."""

from datetime import date

from tests.conftest import utc
from weather_signal import asof


def test_live_minute_30():
    assert asof.live_as_of(utc(2026, 11, 18, 7, 5, 12)) == utc(2026, 11, 18, 7, 30)


def test_backtest_2130_after_every_close(cal):
    for d in cal.between(date(2013, 1, 2), date(2035, 12, 31)):
        a = asof.backtest_as_of(d)
        assert (a.hour, a.minute) == (21, 30)
        assert a > cal.close(d)
    assert asof.backtest_as_of(date(2026, 11, 27)) == utc(2026, 11, 27, 21, 30)
