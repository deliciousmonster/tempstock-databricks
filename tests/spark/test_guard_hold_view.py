"""`v_guard_hold` returns no verified instant while any guard row has none."""

from datetime import datetime

from weather_signal.catalog import setup
from weather_signal.config import Names

N = Names("workspace", "dev_")


def _hold(spark, rows):
    schema = "table_name STRING, open_incident BOOLEAN, post_verified_as_of TIMESTAMP"
    spark.createDataFrame(rows, schema).createOrReplaceTempView("guard_state")
    view = setup.views_and_functions(N)[0]
    select = view.split(" AS ", 1)[1].replace(f"{N.schema('ops')}.guard_state", "guard_state")
    return spark.sql(select).first()


def test_a_fresh_row_without_verification_holds(spark):
    t = datetime(2026, 12, 2, 12, 30)
    got = _hold(spark, [("signal", False, t), ("recommendations", False, None)])
    assert got["open_incident"] is False and got["post_verified_as_of"] is None


def test_all_verified_gives_the_common_instant(spark):
    t = datetime(2026, 12, 2, 12, 30)
    got = _hold(spark, [("signal", False, t), ("recommendations", False, t)])
    assert got["post_verified_as_of"] == t
