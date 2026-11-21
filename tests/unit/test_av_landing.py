"""An Alpha Vantage call is `landed` only when its envelope is in the volume."""

from datetime import UTC, datetime

import pytest

from tests.conftest import fixture_bytes, utc
from tests.unit.fakes import FakeStore
from weather_signal import config
from weather_signal.ingest import hourly
from weather_signal.ingest.fetch import RawResponse

N = config.Names("workspace", "dev_")
SLOT = utc(2026, 11, 17, 22)


class Src:
    endpoint = "https://www.alphavantage.co/query"

    def params(self, ticker):
        return {"function": "TIME_SERIES_DAILY", "symbol": ticker, "outputsize": "compact"}

    def fetch(self, ticker):
        body = fixture_bytes("av-wmt-compact.json")
        return RawResponse(200, {}, body, datetime(2026, 11, 17, 22, 0, tzinfo=UTC))


class RawPut(FakeStore):
    def __init__(self, answer: str) -> None:
        super().__init__()
        self.answer = answer

    def put_if_absent(self, path, data):
        if "/raw/" in path and self.answer != "created":
            return self.answer
        return super().put_if_absent(path, data)


@pytest.mark.parametrize(
    ("put", "outcome"), [("created", "landed"), ("exists", "landed"), ("unknown", "not_landed")]
)
def test_landed_only_after_the_put(cal, put, outcome):
    res = hourly.av_call(RawPut(put), N, cal, Src(), SLOT)("WMT")
    assert res.outcome == outcome
    assert (res.newest_bar is not None) == (outcome == "landed")
