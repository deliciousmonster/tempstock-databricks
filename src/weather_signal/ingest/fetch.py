"""One HTTP fetch with a bounded retry policy."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import requests


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    fetched_at: datetime


class Transport(Protocol):
    def __call__(
        self, url: str, params: Mapping[str, str], timeout: tuple[float, float]
    ) -> RawResponse: ...


class FetchFailed(Exception):
    """Every attempt failed; the message never carries the URL or its parameters."""

    def __init__(self, source: str, reason: str, last: RawResponse | None = None) -> None:
        super().__init__(f"{source}: {reason}")
        self.reason = reason
        self.last = last


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    delays: tuple[float, ...] = (2.0, 8.0, 30.0)
    retry_after_cap: float = 120.0
    timeout: tuple[float, float] = (30.0, 60.0)
    jitter: float = 0.25

    def retryable(self, status: int) -> bool:
        return status == 429 or status >= 500


OPEN_METEO = RetryPolicy()
ONE_SHOT = RetryPolicy(attempts=1, delays=())


@dataclass
class RequestsTransport:
    session: requests.Session = field(default_factory=requests.Session)
    user_agent: str = "weather-signal/0.1"

    def __call__(
        self, url: str, params: Mapping[str, str], timeout: tuple[float, float]
    ) -> RawResponse:
        r = self.session.get(
            url, params=dict(params), timeout=timeout, headers={"User-Agent": self.user_agent}
        )
        return RawResponse(r.status_code, dict(r.headers), r.content, datetime.now(UTC))


def _wait(policy: RetryPolicy, attempt: int, resp: RawResponse | None, rng: random.Random) -> float:
    base = policy.delays[min(attempt, len(policy.delays) - 1)] if policy.delays else 0.0
    if resp is not None and "Retry-After" in resp.headers:
        try:
            base = max(base, float(resp.headers["Retry-After"]))
        except ValueError:
            pass
    return min(base, policy.retry_after_cap) * (1 + rng.uniform(0, policy.jitter))


def fetch(
    source: str,
    url: str,
    params: Mapping[str, str],
    *,
    policy: RetryPolicy,
    transport: Transport,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> RawResponse:
    """Return the first 2xx response; retry 429, 5xx, timeouts and connect errors only."""
    rng = rng or random.Random()
    last: RawResponse | None = None
    reason = "no attempt"
    for attempt in range(policy.attempts):
        if attempt:
            sleep(_wait(policy, attempt - 1, last, rng))
        try:
            resp = transport(url, params, policy.timeout)
        except (requests.Timeout, TimeoutError):
            last, reason = None, "timeout"
            continue
        except requests.ConnectionError:
            last, reason = None, "unreachable"
            continue
        except requests.RequestException as err:
            last, reason = None, type(err).__name__
            break
        except OSError:
            last, reason = None, "unreachable"
            continue
        if 200 <= resp.status < 300:
            return resp
        last, reason = resp, f"http {resp.status}"
        if not policy.retryable(resp.status):
            break
    raise FetchFailed(source, reason, last)
