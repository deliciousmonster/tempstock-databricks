"""The traded stock and its benchmarks live in `config` alone; feature keys name the primary by role."""

from datetime import date

from weather_signal import config


def test_primary_benchmarks_and_tickers():
    assert config.PRIMARY == "WMT" and config.BENCHMARKS == ("SPY", "XLP")
    assert config.TICKERS == ("WMT", "SPY", "XLP")
    assert config.HISTORY_START == date(1991, 1, 1)


def test_each_benchmark_first_date_is_a_session(cal):
    assert set(config.BENCHMARK_FIRST) == set(config.BENCHMARKS)
    for t, first in config.BENCHMARK_FIRST.items():
        assert cal.between(first, first) == [first], t
    assert config.BENCHMARK_FIRST == {"SPY": date(1993, 1, 29), "XLP": date(1998, 12, 22)}


def test_keys_and_model_name_are_neutral():
    assert [config.key(t) for t in config.TICKERS] == ["stock", "spy", "xlp"]
    assert config.MODEL_NAME == "stock_signal"
