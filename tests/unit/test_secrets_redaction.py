"""No envelope, log line or raised error carries the Alpha Vantage key."""

import logging

import pytest
import requests

from tests.conftest import utc
from weather_signal.ingest import envelope
from weather_signal.ingest.fetch import FetchFailed, RawResponse
from weather_signal.ingest.sources import AlphaVantageDaily
from weather_signal.redaction import redact

KEY = "SEKRIT0123456789"


def test_envelope_carries_no_key():
    src = AlphaVantageDaily(KEY, lambda *a: None)
    resp = RawResponse(200, {}, b'{"x": 1}', utc(2026, 11, 18))
    env = envelope.build("alphavantage", src.endpoint, src.params("WMT"), "s", resp, host="h")
    assert KEY not in envelope.encode(env).decode("latin-1") and KEY not in str(env)


@pytest.mark.parametrize(
    "err",
    [
        requests.ConnectionError(f"url?apikey={KEY}"),
        requests.HTTPError(f"https://x/query?apikey={KEY}"),
    ],
)
def test_error_and_log_carry_no_key(err, caplog):
    def transport(url, params, timeout):
        raise err

    src = AlphaVantageDaily(KEY, transport)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(FetchFailed) as e:
        src.fetch("WMT")
    logging.getLogger("t").warning("failed: %s", e.value)
    assert KEY not in str(e.value) and KEY not in repr(e.value.args)
    assert KEY not in caplog.text
    assert e.value.__cause__ is None or KEY not in str(e.value.__cause__)


def test_redact():
    assert KEY not in redact(f"https://x?apikey={KEY}", KEY)
