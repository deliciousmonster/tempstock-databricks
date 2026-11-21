"""Yahoo daily and Alpha Vantage compact payloads parse to the same columns and the same closes.

The fixtures are synthetic: the recorded dates and document shapes, with seeded random-walk prices
(`tests/fixtures/synthetic_prices.py`). Both WMT bodies share their closes on the overlapping dates.
"""

from datetime import date

import pytest

from tests.conftest import fixture_json
from weather_signal.ingest import price_bars as pb


@pytest.mark.parametrize(
    ("t", "f", "last"),
    [("WMT", "av-wmt-compact.json", "2026-11-19"), ("SPY", "av-spy-compact.json", "2026-11-16")],
)
def test_alpha_vantage_compact(t, f, last):
    bars = pb.alpha_vantage(t, fixture_json(f))
    assert len(bars) == 100 and {b.ticker for b in bars} == {t}
    assert str(bars[-1].trade_date) == last
    assert all(b.low <= b.close <= b.high for b in bars)


def test_yahoo_unadjusted_close_matches_alpha_vantage():
    av = {
        b.trade_date: b.close for b in pb.alpha_vantage("WMT", fixture_json("av-wmt-compact.json"))
    }
    ya = pb.yahoo("WMT", fixture_json("yahoo-wmt-recent.json"))
    shared = [b for b in ya if b.trade_date in av]
    assert len(shared) >= 30
    assert max(abs(b.close / av[b.trade_date] - 1) for b in shared) < 1e-4


def test_yahoo_reads_close_not_adjclose():
    # The recorded 60 bars hold no dividend, so adjclose equals close; plant one that differs.
    doc = fixture_json("yahoo-wmt-recent.json")
    ind = doc["chart"]["result"][0]["indicators"]
    close = ind["quote"][0]["close"]
    ind["adjclose"] = [{"adjclose": [c * 0.99 if c else c for c in close]}]
    bars = pb.yahoo("WMT", doc)
    assert [b.close for b in bars] == [c for c in close if c is not None][: len(bars)]


def test_from_envelope_keeps_every_bar_for_the_bounds():
    from tests.conftest import fixture_bytes

    body = fixture_bytes("av-wmt-compact.json").decode()
    bars = pb.from_envelope("alphavantage", "x", {"symbol": "WMT"}, body)
    assert len(bars) == 100 and str(bars[-1].trade_date) == "2026-11-19"
    ya = pb.from_envelope(
        "yahoo",
        "https://h/v8/finance/chart/WMT",
        {},
        fixture_bytes("yahoo-wmt-recent.json").decode(),
    )
    assert {b.ticker for b in ya} == {"WMT"}


def test_yahoo_wmt_reads_the_unadjusted_close_from_1991():
    # Synthetic bars on the recorded dates and shape.
    bars = {b.trade_date: b.close for b in pb.yahoo("WMT", fixture_json("yahoo-wmt-daily.json"))}
    assert min(bars) == date(1991, 1, 2) and bars[date(1991, 1, 2)] == pytest.approx(2.98)
    assert bars[date(2024, 2, 26)] == pytest.approx(59.78, abs=0.005)
    assert date(1999, 4, 19) in bars and len(bars) == 42
