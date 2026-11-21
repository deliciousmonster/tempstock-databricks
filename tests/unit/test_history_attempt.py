"""One Yahoo history attempt: a full history lands once under its span; anything short lands nothing."""

import json
from datetime import UTC, date, datetime, time

from tests.conftest import utc
from tests.unit.fakes import FakeStore
from weather_signal import config
from weather_signal.ingest import envelope
from weather_signal.ingest import yahoo_history as h
from weather_signal.ingest.fetch import RawResponse

N = config.Names("workspace", "dev_")
NOW = utc(2026, 11, 18, 4, 20)


def chart(ticker, days):
    ts = [int(datetime.combine(d, time(14, 30), tzinfo=UTC).timestamp()) for d in days]
    quote = {"close": [100.0] * len(ts)}
    result = {"meta": {"symbol": ticker}, "timestamp": ts, "indicators": {"quote": [quote]}}
    return json.dumps({"chart": {"result": [result]}}).encode()


class Yahoo:
    def __init__(self, reply):
        self.reply, self.calls, self.params = reply, [], []

    def __call__(self, url, params, timeout):
        ticker = url.rsplit("/", 1)[1]
        self.calls.append(ticker)
        self.params.append(params)
        status, body = self.reply(ticker)
        return RawResponse(status, {}, body, NOW)


def full(cal):
    """Each ticker's body from its own first session, as Yahoo answers it."""
    return lambda t: (200, chart(t, cal.between(h.start_for(t), cal.latest_closed_before(NOW))))


def test_a_full_history_lands_once_under_its_span(cal):
    store, srv = FakeStore(), Yahoo(full(cal))
    got = h.attempt_for(store, N, srv, cal, NOW)("SPY")
    assert got[0] == "valid"
    (path,) = store.files
    assert path.startswith(f"{N.raw}/yahoo/1993/01/29/00/")
    env = envelope.decode(store.files[path])
    assert env["slot"].startswith("1993-01-29..") and env["source"] == "yahoo"
    assert env["request"]["params"]["period1"] == str(int(utc(1993, 1, 29).timestamp()))
    assert srv.params[0]["period1"] == env["request"]["params"]["period1"]


def test_a_benchmark_body_from_before_its_first_session_is_invalid(cal):
    early = cal.between(date(1991, 1, 2), cal.latest_closed_before(NOW))
    store = FakeStore()
    got = h.attempt_for(store, N, Yahoo(lambda t: (200, chart(t, early))), cal, NOW)("SPY")
    assert got[0] == "invalid" and "1991-01-02" in got[1] and "1993-01-29" in got[1]
    assert store.files == {}


def test_a_ticker_without_a_benchmark_date_starts_at_the_history_start(cal):
    store = FakeStore()
    assert h.attempt_for(store, N, Yahoo(full(cal)), cal, NOW)("WMT")[0] == "valid"
    (path,) = store.files
    assert path.startswith(f"{N.raw}/yahoo/1991/01/01/00/")
    assert h.start_for("XLP") == date(1998, 12, 22) and h.start_for("WMT") == h.HISTORY_START


def test_429_is_throttled_and_lands_nothing(cal):
    store = FakeStore()
    got = h.attempt_for(store, N, Yahoo(lambda t: (429, b"Too Many Requests")), cal, NOW)("WMT")
    assert got == ("throttled", "http 429") and store.files == {}


def test_a_short_history_is_invalid_not_throttled(cal):
    store = FakeStore()
    recent = cal.between(date(2026, 10, 1), cal.latest_closed_before(NOW))
    got = h.attempt_for(store, N, Yahoo(lambda t: (200, chart(t, recent))), cal, NOW)("WMT")
    assert (
        got[0] == "invalid" and "after the first session 1991-01-02" in got[1] and store.files == {}
    )


def test_an_unknown_landing_is_retried_not_marked_landed(cal):
    store = FakeStore()
    srv = Yahoo(full(cal))
    attempt = h.attempt_for(store, N, srv, cal, NOW)
    attempt("SPY")
    (path,) = store.files
    store.files.clear()
    store.unknown_puts.add(path)
    assert attempt("SPY")[0] == "throttled"
