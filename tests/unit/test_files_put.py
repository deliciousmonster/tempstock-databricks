"""`put_if_absent` classifies on the status alone and never parses the body."""

import pytest
import requests

from weather_signal.ingest.files import Files


class Client:
    def __init__(self, result):
        self.result = result

    def put(self, url, data, headers, timeout):
        assert "overwrite=false" in url
        if isinstance(self.result, Exception):
            raise self.result
        status, _body = self.result
        return status


@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        ((204, b""), "created"),
        ((409, b'{"error_code":"ALREADY_EXISTS"}'), "exists"),
        ((409, b"409 Conflict"), "exists"),
        ((500, b'{"error_code":"ALREADY_EXISTS"}'), "unknown"),
        (requests.Timeout(), "unknown"),
    ],
)
def test_status_only(result, outcome):
    f = Files("https://h", lambda: {}, Client(result))
    assert f.put_if_absent("/Volumes/c/s/v/x.json.gz", b"x") == outcome


class Deleting:
    def __init__(self, result):
        self.result = result

    def delete(self, url, headers, timeout):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.parametrize(
    ("result", "gone"),
    [(204, True), (404, True), (403, False), (500, False), (requests.ConnectionError(), False)],
)
def test_delete_says_gone_only_on_2xx_or_404(result, gone):
    f = Files("https://h", lambda: {}, Deleting(result))
    assert f.delete("/Volumes/c/s/v/x") is gone
