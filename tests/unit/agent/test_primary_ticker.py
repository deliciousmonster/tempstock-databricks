"""The agent prices, describes and prompts the stock `config.PRIMARY` names."""

from __future__ import annotations

from weather_signal import config
from weather_signal.agent import options, tools
from weather_signal.agent.store import SparkStore


class _Row:
    def __init__(self, d):
        self.d = d

    def asDict(self):
        return self.d


class _Spark:
    def __init__(self):
        self.args = []

    def sql(self, q, args=None):
        self.args.append(args)
        rows = [_Row({"trade_date": "2026-11-17", "close": 101.5, "known_at": "x"})]
        return type("R", (), {"collect": lambda _self: rows})()


def test_prices_bind_and_carry_the_primary():
    spark = _Spark()
    got = SparkStore(spark, config.Names()).prices()
    assert spark.args[0]["t"] == config.PRIMARY
    assert got == [
        {"trade_date": "2026-11-17", "close": 101.5, "known_at": "x", "ticker": config.PRIMARY}
    ]
    assert not any(k.endswith("_close") for k in got[0])


def test_prompt_and_tool_name_the_primary():
    text = options.prompt_text()
    assert f"for {config.PRIMARY} stock" in text
    assert "{" not in text  # Every placeholder filled, not line 1 alone
    assert config.PRIMARY in tools.PRICES_DOC
