"""Yahoo 60m bars for display: the session window, the hour grid, retention and the landing."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
import requests

from tests.conftest import utc
from tests.unit.fakes import FakeStore
from tests.unit.yahoo_60m import body, session_points
from weather_signal import config
from weather_signal.ingest import envelope, intraday
from weather_signal.ingest.fetch import RawResponse

N = config.Names("workspace", "dev_")
DAYS = [date(2026, 11, d) for d in (16, 17, 18, 19, 20)]
NOW = utc(2026, 11, 20, 18, 5)


@pytest.mark.parametrize(
    ("now", "inside"),
    [
        (utc(2026, 11, 20, 14, 29), False),  # before the open
        (utc(2026, 11, 20, 14, 30), True),
        (utc(2026, 11, 20, 21, 59), True),  # within an hour of the close
        (utc(2026, 11, 20, 22, 0), False),
        (utc(2026, 11, 21, 15, 5), False),  # Saturday
        (utc(2026, 11, 26, 15, 5), False),  # Thanksgiving
        (utc(2026, 11, 27, 18, 59), True),  # early close 18:00Z, from the seed
        (utc(2026, 11, 27, 19, 5), False),
    ],
)
def test_the_window_is_the_seed_session_to_an_hour_past_its_close(cal, now, inside):
    assert intraday.in_window(now, cal) is inside


def test_retention_starts_at_the_open_of_the_fifth_session_back(cal):
    assert intraday.keep_from(NOW, cal) == utc(2026, 11, 16, 14, 30)
    # Over Thanksgiving: 11-27 counts, 11-26 does not.
    assert intraday.keep_from(utc(2026, 11, 27, 15, 5), cal) == utc(2026, 11, 20, 14, 30)
    assert intraday.keep_from(utc(2026, 11, 22, 12, 0), cal) == utc(2026, 11, 16, 14, 30)


def _bars(cal, points, fetched_at=NOW):
    return intraday.bars(json.loads(body(points)), fetched_at, cal)


def test_bars_sit_on_the_session_grid_and_the_trailing_quote_is_dropped(cal):
    pts = session_points(cal, DAYS, NOW)
    got = _bars(cal, [*pts, (NOW - timedelta(seconds=4), 101.0)])
    assert [b.bar_start for b in got] == [t for t, _ in pts]
    assert all(b.bar_start.minute == 30 for b in got) and len(got) == 4 * 7 + 4


def test_off_grid_out_of_session_and_closeless_points_are_not_bars(cal):
    pts = [
        (utc(2026, 11, 20, 14, 0), 99.0),  # before the open
        (utc(2026, 11, 20, 14, 30), None),  # no close
        (utc(2026, 11, 20, 15, 0), 99.0),  # off the grid
        (utc(2026, 11, 21, 15, 30), 99.0),  # not a session
        (utc(2026, 11, 20, 15, 30), 100.0),
    ]
    assert [b.bar_start for b in _bars(cal, pts)] == [utc(2026, 11, 20, 15, 30)]


def test_final_marks_a_complete_bar_and_the_running_one_is_not(cal):
    pts = session_points(cal, [date(2026, 11, 20)], NOW)
    got = {b.bar_start.hour: b.final for b in _bars(cal, pts)}
    assert got == {14: True, 15: True, 16: True, 17: False}
    # The last bar ends at the close, not an hour after its start; the early close too.
    late = _bars(cal, session_points(cal, [date(2026, 11, 20)], NOW + timedelta(hours=4)),
                 utc(2026, 11, 20, 21, 5))  # fmt: skip
    assert late[-1].bar_start == utc(2026, 11, 20, 20, 30) and late[-1].final
    early = _bars(cal, session_points(cal, [date(2026, 11, 27)], utc(2026, 11, 27, 18)),
                  utc(2026, 11, 27, 18, 0))  # fmt: skip
    assert early[-1].bar_start == utc(2026, 11, 27, 17, 30) and early[-1].final


def test_retained_drops_bars_before_the_fifth_session_back(cal):
    pts = session_points(cal, [date(2026, 11, 13), *DAYS], NOW)
    got = intraday.retained(body(pts).decode(), NOW, cal)
    assert got[0].bar_start == utc(2026, 11, 16, 14, 30)


def test_udf_rows_carry_epoch_seconds_and_the_envelopes_keep_from(cal):
    pts = session_points(cal, [date(2026, 11, 20)], NOW)
    rows = intraday.udf_rows(body(pts).decode(), int(NOW.timestamp()), cal)
    first = rows[0]
    assert first[0] == "WMT" and first[1] == int(utc(2026, 11, 20, 14, 30).timestamp())
    assert first[5] == 100.0 and first[6] == 1000 and first[7] is True
    assert {r[8] for r in rows} == {int(utc(2026, 11, 16, 14, 30).timestamp())}


class Transport:
    def __init__(self, answer):
        self.answer = answer
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, params, timeout):
        self.calls.append((url, dict(params)))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _ok(cal, fetched_at=NOW):
    return RawResponse(200, {}, body(session_points(cal, DAYS, fetched_at)), fetched_at)


def test_outside_the_window_there_is_no_call(cal):
    t, store = Transport(_ok(cal)), FakeStore()
    assert intraday.fetch_and_land(store, N, cal, t, utc(2026, 11, 21, 15, 5)) == "closed"
    assert t.calls == [] and store.puts == []


def test_a_session_hour_lands_one_envelope_under_its_own_source(cal):
    t, store = Transport(_ok(cal)), FakeStore()
    assert intraday.fetch_and_land(store, N, cal, t, NOW) == "created"
    assert t.calls == [
        (intraday.sources.YAHOO.format(ticker="WMT"), {"interval": "60m", "range": "5d"})
    ]
    (path,) = store.files
    assert path.startswith("/Volumes/workspace/dev_raw/raw/yahoo-intraday/2026/11/20/18/")
    env = envelope.decode(store.files[path])
    assert env["source"] == "yahoo-intraday" and env["slot"] == "2026-11-20T18:00:00+00:00"
    assert intraday.fetch_and_land(store, N, cal, t, NOW) == "exists"


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (RawResponse(503, {}, b"busy", NOW), "fetch_failed"),
        (requests.ConnectionError("down"), "fetch_failed"),
        (RawResponse(200, {}, b"<html>", NOW), "invalid"),
        (RawResponse(200, {}, b'{"chart": {"result": null}}', NOW), "invalid"),
        (RawResponse(200, {}, body([(utc(2026, 11, 20, 18, 1), 1.0)]), NOW), "invalid"),
    ],
)
def test_a_failed_fetch_logs_and_lands_nothing(cal, monkeypatch, caplog, answer, outcome):
    monkeypatch.setattr(intraday, "POLICY", intraday.RetryPolicy(attempts=1, delays=()))
    store = FakeStore()
    assert intraday.fetch_and_land(store, N, cal, Transport(answer), NOW) == outcome
    assert store.puts == []
    assert any(
        r.levelname == "WARNING" and "intraday WMT" in r.getMessage() for r in caplog.records
    )


def test_an_unknown_put_is_reported_not_raised(cal):
    class Unknown(FakeStore):
        def put_if_absent(self, path, data):
            return "unknown"

    assert intraday.fetch_and_land(Unknown(), N, cal, Transport(_ok(cal)), NOW) == "unknown"


def test_the_job_exits_clean_when_the_fetch_fails(monkeypatch, cal):
    from weather_signal.jobs import intraday as job

    store = FakeStore()
    monkeypatch.setattr(job.Files, "from_sdk", classmethod(lambda cls: store))
    monkeypatch.setattr(job, "RequestsTransport", lambda: Transport(requests.ConnectionError("x")))
    monkeypatch.setattr(intraday, "POLICY", intraday.RetryPolicy(attempts=1, delays=()))
    monkeypatch.setattr(job, "datetime", _Frozen)
    assert job.main(["--schema-prefix", "dev_"]) == 0
    assert store.puts == []


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


HUGE_TS = (
    '{"chart":{"result":[{"timestamp":[100000000000000000000],"indicators":{"quote":'
    '[{"close":[1],"open":[1],"high":[1],"low":[1],"volume":[1]}]}}]}}'
)


def _infinite_volume(cal) -> str:
    doc = json.loads(body(session_points(cal, [date(2026, 11, 20)], NOW)))
    doc["chart"]["result"][0]["indicators"]["quote"][0]["volume"][0] = float("inf")
    return json.dumps(doc)


@pytest.mark.parametrize("text", ["", "<html>", '{"chart": {"result": null}}', HUGE_TS, "inf"])
def test_an_unreadable_envelope_gives_the_pipeline_no_rows_and_a_log(cal, caplog, text):
    text = _infinite_volume(cal) if text == "inf" else text
    assert intraday.udf_rows(text, int(NOW.timestamp()), cal) == []
    assert "intraday envelope unreadable" in caplog.text


@pytest.mark.parametrize("text", [HUGE_TS, "inf"])
def test_an_overflowing_body_is_invalid_and_the_job_still_exits_0(cal, monkeypatch, text):
    from weather_signal.jobs import intraday as job

    raw = (_infinite_volume(cal) if text == "inf" else text).encode()
    resp = RawResponse(200, {}, raw, NOW)
    assert intraday.fetch_and_land(FakeStore(), N, cal, Transport(resp), NOW) == "invalid"
    store = FakeStore()
    monkeypatch.setattr(job.Files, "from_sdk", classmethod(lambda cls: store))
    monkeypatch.setattr(job, "RequestsTransport", lambda: Transport(resp))
    monkeypatch.setattr(job, "datetime", _Frozen)
    assert job.main(["--schema-prefix", "dev_"]) == 0 and store.puts == []
