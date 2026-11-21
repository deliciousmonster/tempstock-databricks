"""ERA5 is first seen only when all three variables are present for every metro; a bar's first envelope wins."""

import copy
from datetime import date

from tests.conftest import fixture_json
from tests.spark.conftest import ts
from tests.spark.helpers import envelopes
from weather_signal.pipeline import transforms


def _fifty(doc):
    return [copy.deepcopy(doc) for _ in range(50)]


def test_era5_needs_all_three_variables(spark, metros):
    early = fixture_json("era5-now.json")  # 11-11: mean present, max and min null
    later = copy.deepcopy(early)
    d = later["daily"]
    d["temperature_2m_max"][8] = 14.0
    d["temperature_2m_min"][8] = 7.0
    env = envelopes(
        spark,
        [
            ("a", ts(2026, 11, 17, 3, 5), _fifty(early)),
            ("b", ts(2026, 11, 18, 3, 5), _fifty(later)),
        ],
    )
    seen = {
        r["date"]: r["known_at"]
        for r in transforms.era5_first_seen(transforms.era5_days(env, metros)).collect()
    }
    assert seen[date(2026, 11, 10)] == ts(2026, 11, 17, 3, 5)
    assert seen[date(2026, 11, 11)] == ts(2026, 11, 18, 3, 5)
    assert date(2026, 11, 12) not in seen


def test_price_bar_first_envelope_wins(spark):
    df = spark.createDataFrame(
        [
            ("alphavantage", "WMT", date(2026, 11, 16), ts(2026, 11, 17, 2, 5)),
            ("alphavantage", "WMT", date(2026, 11, 16), ts(2026, 11, 16, 22, 5)),
            ("alphavantage", "WMT", date(2026, 11, 16), ts(2026, 11, 17, 9, 5)),
        ],
        "source string, ticker string, trade_date date, fetched_at timestamp",
    )
    assert transforms.price_first_seen(df).first()["first_seen"] == ts(2026, 11, 16, 22, 5)
