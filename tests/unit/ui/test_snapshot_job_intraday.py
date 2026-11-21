"""The snapshot job reads the intraday view, and its absence leaves the rest of the snapshot whole."""

from __future__ import annotations

from weather_signal.config import Names
from weather_signal.jobs import snapshot as job
from weather_signal.serving import views

N = Names("workspace", "dev_")


class Row(dict):
    def asDict(self, recursive=False):
        return dict(self)


class Spark:
    def __init__(self, missing: bool):
        self.missing = missing
        self.read: list[str] = []

    def sql(self, q):
        self.read.append(q)
        if self.missing and views.INTRADAY_VIEW in q:
            raise RuntimeError("TABLE_OR_VIEW_NOT_FOUND")
        return self

    def collect(self):
        return [Row(x=1)]


def test_the_intraday_view_is_read_beside_the_six():
    spark = Spark(missing=False)
    got = job.read_views(spark, N)
    assert set(got) == {*views.VIEWS, views.INTRADAY_VIEW}
    assert "SELECT * FROM workspace.dev_app.v_ui_intraday" in spark.read


def test_a_missing_intraday_view_is_an_empty_chart_not_a_failed_snapshot(caplog):
    got = job.read_views(Spark(missing=True), N)
    assert got[views.INTRADAY_VIEW] == [] and all(got[v] == [{"x": 1}] for v in views.VIEWS)
    assert "intraday not read: RuntimeError" in caplog.text
