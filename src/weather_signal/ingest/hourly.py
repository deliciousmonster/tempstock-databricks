"""The hourly ingest's steps: each source fetched, validated and landed once; `jobs.ingest` runs them."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from weather_signal import config
from weather_signal.ingest import av_ledger as av
from weather_signal.ingest import envelope, sources, validate
from weather_signal.ingest.fetch import FetchFailed, RawResponse, Transport
from weather_signal.ingest.files import Files, Outcome, Store
from weather_signal.sessions import Calendar

log = logging.getLogger(__name__)
ERA5_HOUR = 3
ERA5_DAYS = 14


def land(
    files: Store,
    root: str,
    source: str,
    endpoint: str,
    params: Mapping[str, str],
    slot: str,
    slot_time: datetime,
    resp: RawResponse,
    flag: str | None = None,
) -> Outcome:
    env: dict[str, Any] = envelope.build(source, endpoint, params, slot, resp, host=_host(endpoint))
    if flag:
        env["flag"] = flag
    key = envelope.landing_key(source, endpoint, params, slot)
    return files.put_if_absent(
        envelope.landing_path(root, source, slot_time, key), envelope.encode(env)
    )


def _host(url: str) -> str:
    return url.split("/")[2]


def weather_hour(
    files: Files,
    n: config.Names,
    now: datetime,
    transport: Transport,
    p_hours: int,
    *,
    only_if_missing: bool = False,
) -> str:
    slot_time = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    params = sources.previous_runs_params(sources.metros())
    slot = slot_time.isoformat()
    key = envelope.landing_key("open-meteo-previous-runs", sources.PREVIOUS_RUNS, params, slot)
    path = envelope.landing_path(n.raw, "open-meteo-previous-runs", slot_time, key)
    if only_if_missing and files.exists(path) == "present":
        return "present"
    try:
        resp = sources.open_meteo(sources.PREVIOUS_RUNS, params, transport)
    except FetchFailed as err:
        log.warning("previous runs not fetched: %s", err.reason)
        return "fetch_failed"
    v = validate.previous_runs(resp.body, resp.fetched_at, p_hours)
    if not v.ok:
        log.warning("previous runs not landed: %s", v.reason)
        return "invalid"
    return land(
        files,
        n.raw,
        "open-meteo-previous-runs",
        sources.PREVIOUS_RUNS,
        params,
        slot,
        slot_time,
        resp,
    )


def era5_day(files: Files, n: config.Names, now: datetime, transport: Transport) -> str:
    end = now.date() - timedelta(days=1)
    start = end - timedelta(days=ERA5_DAYS - 1)
    params = sources.era5_params(sources.metros(), start, end)
    day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        resp = sources.open_meteo(sources.ARCHIVE, params, transport)
    except FetchFailed as err:
        log.warning("era5 not fetched: %s", err.reason)
        return "fetch_failed"
    v = validate.era5_steady(resp.body)
    if not v.ok:
        log.warning("era5 not landed: %s", v.reason)
        return "invalid"
    return land(
        files,
        n.raw,
        "open-meteo-era5",
        sources.ARCHIVE,
        params,
        day.date().isoformat(),
        day,
        resp,
        flag=v.flag,
    )


def _outcome(err: FetchFailed) -> av.CallOutcome:
    if err.reason == "timeout":
        return "timeout"
    if err.reason == "unreachable":
        return "unreachable"
    return "invalid"


def av_call(
    files: Files,
    n: config.Names,
    cal: Calendar,
    src: sources.AlphaVantageDaily,
    slot_time: datetime,
) -> Callable[[str], av.CallResult]:
    def call(ticker: str) -> av.CallResult:
        try:
            resp = src.fetch(ticker)
        except FetchFailed as err:
            log.warning("alphavantage %s: %s", ticker, err.reason)
            return av.CallResult(_outcome(err))
        doc = validate.parse_json(resp.body)
        v = validate.prices(resp.body, resp.fetched_at, cal)
        if not v.ok:
            note = isinstance(doc, dict) and bool({"Note", "Information"} & set(doc))
            log.warning("alphavantage %s not landed: %s", ticker, "rate note" if note else v.reason)
            return av.CallResult("rate_note" if note else "invalid")
        params = src.params(ticker)
        put = land(
            files,
            n.raw,
            "alphavantage",
            src.endpoint,
            params,
            slot_time.isoformat(),
            slot_time,
            resp,
        )
        if put == "unknown":
            log.warning("alphavantage %s fetched but not landed: the PUT was unknown", ticker)
            return av.CallResult("not_landed")
        dates = validate.price_dates(doc) or []
        return av.CallResult("landed", dates[-1] if dates else None)

    return call


def alpha_vantage_slot(
    files: Files, n: config.Names, cal: Calendar, key: str, transport: Transport, clock: av.Clock
) -> av.SlotReport:
    slot_time = clock.now().replace(minute=0, second=0, microsecond=0)
    src = sources.AlphaVantageDaily(key, transport)
    return av.run_slot(
        av.Ledger(n.ledger, files), cal, clock, av_call(files, n, cal, src, slot_time)
    )
