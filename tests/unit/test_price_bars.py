"""Completeness comes from the calendar's close; both sources parse to one shape."""

from datetime import date

from tests.conftest import fixture_json, utc
from weather_signal.ingest import price_bars as pb


def test_half_day_bar_complete_after_its_early_close(cal):
    d = date(2026, 11, 27)
    assert not pb.complete(d, utc(2026, 11, 27, 18, 10), cal)
    assert pb.complete(d, utc(2026, 11, 27, 18, 20), cal)


def test_full_day_bar_needs_close_plus_15_minutes(cal):
    d = date(2026, 11, 17)
    assert not pb.complete(d, utc(2026, 11, 17, 21, 10), cal)
    assert pb.complete(d, utc(2026, 11, 17, 21, 15), cal)


def test_both_sources_one_shape_and_agree():
    av = {b.trade_date: b for b in pb.alpha_vantage("WMT", fixture_json("av-wmt-compact.json"))}
    ya = {b.trade_date: b for b in pb.yahoo("WMT", fixture_json("yahoo-wmt-recent.json"))}
    shared = sorted(set(av) & set(ya))
    assert len(shared) >= 30
    for d in shared:
        assert abs(av[d].close / ya[d].close - 1) < 0.005, d
