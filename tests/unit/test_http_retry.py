"""Open-Meteo retries 429, 5xx and timeouts three times; never a 404."""

import pytest
import requests

from tests.conftest import utc
from weather_signal.ingest.fetch import ONE_SHOT, OPEN_METEO, FetchFailed, RawResponse, fetch


class Script:
    def __init__(self, *steps):
        self.steps, self.calls = list(steps), 0

    def __call__(self, url, params, timeout):
        self.calls += 1
        s = self.steps.pop(0)
        if isinstance(s, Exception):
            raise s
        return RawResponse(s, {}, b"{}", utc(2026, 11, 18))


@pytest.mark.parametrize("first", [429, 500, 503, requests.Timeout(), requests.ConnectionError()])
def test_retryable_then_success(first):
    t = Script(first, first, 200)
    slept = []
    assert fetch("om", "u", {}, policy=OPEN_METEO, transport=t, sleep=slept.append).status == 200
    assert t.calls == 3 and len(slept) == 2


def test_three_attempts_at_most():
    t = Script(500, 500, 500, 200)
    with pytest.raises(FetchFailed) as e:
        fetch("om", "u", {}, policy=OPEN_METEO, transport=t, sleep=lambda s: None)
    assert t.calls == 3 and e.value.reason == "http 500"


@pytest.mark.parametrize("status", [400, 404])
def test_client_errors_are_not_retried(status):
    t = Script(status, 200)
    with pytest.raises(FetchFailed):
        fetch("om", "u", {}, policy=OPEN_METEO, transport=t, sleep=lambda s: None)
    assert t.calls == 1


def test_retry_after_is_capped():
    class R(Script):
        def __call__(self, url, params, timeout):
            self.calls += 1
            if self.calls == 1:
                return RawResponse(429, {"Retry-After": "900"}, b"", utc(2026, 11, 18))
            return RawResponse(200, {}, b"", utc(2026, 11, 18))

    slept = []
    fetch("om", "u", {}, policy=OPEN_METEO, transport=R(), sleep=slept.append)
    assert 120 <= slept[0] <= 120 * 1.25


def test_one_shot_is_one_attempt():
    t = Script(429, 200)
    with pytest.raises(FetchFailed):
        fetch("av", "u", {}, policy=ONE_SHOT, transport=t, sleep=lambda s: None)
    assert t.calls == 1
