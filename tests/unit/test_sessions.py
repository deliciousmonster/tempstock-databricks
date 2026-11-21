"""The generated XNYS calendar reaches 2035-12-31, with its half days and sessions."""

from datetime import date


def test_calendar_reaches_2035(cal):
    assert cal.end == date(2035, 12, 31)
    assert cal.is_session(date(2035, 12, 31))


def test_half_days_close_early(cal):
    assert cal.close(date(2026, 11, 27)).hour == 18
    assert cal.close(date(2025, 7, 3)).hour == 17
    assert cal.close(date(2026, 7, 15)).hour == 20


def test_the_seed_holds_1991_for_the_price_histories(cal):
    assert cal.is_session(date(1990, 12, 31)) and cal.is_session(date(1991, 1, 2))
    assert not cal.is_session(date(1991, 1, 1)) and not cal.is_session(date(2001, 9, 11))
