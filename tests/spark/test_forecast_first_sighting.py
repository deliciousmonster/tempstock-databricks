"""Three hourly envelopes repeating values, in one batch in reverse order: one row per key, earliest fetch."""

from tests.conftest import fixture_json
from tests.spark.conftest import ts
from tests.spark.helpers import envelopes
from weather_signal.pipeline import transforms


def test_first_sighting_per_key(spark, metros):
    body = fixture_json("prevruns-1807.json")
    times = [ts(2026, 11, 17, h, 7) for h in (20, 19, 18)]
    env = envelopes(spark, [(f"e{i}", t, body) for i, t in enumerate(times)])
    landed = transforms.landed_forecast(transforms.forecast_points(env, metros), 30)
    kept = transforms.first_sightings(landed)
    keys = kept.select(*transforms.FORECAST_KEY)
    assert keys.count() == keys.distinct().count()
    assert landed.select(*transforms.FORECAST_KEY).distinct().count() == kept.count()
    first = (
        landed.groupBy(*transforms.FORECAST_KEY)
        .agg({"fetched_at": "min"})
        .withColumnRenamed("min(fetched_at)", "first")
    )
    j = kept.join(first, transforms.FORECAST_KEY)
    assert j.where("fetched_at != first").count() == 0
    assert kept.where("fetched_at = timestamp'2026-11-17 18:07:00'").count() > 0
