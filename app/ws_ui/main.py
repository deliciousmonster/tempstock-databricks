"""The App: a GET-only JSON API over the snapshot and the same pages."""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ws_ui import integrity, measure
from ws_ui.source import Source, Unavailable

log = logging.getLogger("ws_ui")
NOTICE = "Personal demo. Not investment advice. Weather data by Open-Meteo.com (CC BY 4.0)."
STATIC = Path(__file__).resolve().parent.parent / "static"
MAX_RANGE = timedelta(days=400)
BACKTEST_TTL_S = 900
SNAPSHOT_HORIZONS = (0, 6)  # snapshot.py keeps these in `history`
AS_OF = r"^\d{4}-\d{2}-\d{2}T\d{2}:30:00Z$"

app = FastAPI(title="weather-signal", docs_url=None, redoc_url=None, openapi_url=None)
source = Source()
# Off only for a local run; the front door sets x-forwarded-email on every request it lets through.
REQUIRE_USER = os.environ.get("WS_UI_LOCAL") != "1"


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@app.middleware("http")
async def read_only(request: Request, call_next: Any) -> Any:
    """U1: any method but GET (and HEAD) is 405; every route but /healthz needs the front door's user."""
    if request.method not in ("GET", "HEAD"):
        return JSONResponse({"detail": "read-only"}, status_code=405)
    path = request.url.path
    if path != "/healthz" and REQUIRE_USER:
        if not request.headers.get("x-forwarded-email"):
            return JSONResponse({"detail": "unauthenticated"}, status_code=401)
    log.info("%s %s", request.headers.get("x-forwarded-email", "local"), path)
    return await call_next(request)


def with_integrity(call: dict[str, Any], status: dict[str, Any], now: str) -> dict[str, Any]:
    row = {
        **call,
        "open_incident": status.get("open_incident"),
        "post_verified_as_of": status.get("post_verified_as_of"),
        "guarded_tables": status.get("guarded_tables"),
        "read_at": status.get("read_at"),
    }
    out = dict(call)
    out["integrity"] = integrity.state(row, now)
    out["integrity_label"] = integrity.label(out["integrity"], status.get("read_at"))
    out["integrity_at_read"] = integrity.state(row, status.get("read_at"))
    return out


def envelope(data: Any, doc: dict[str, Any] | None, src: str = "snapshot") -> dict[str, Any]:
    status = (doc or {}).get("payload", {}).get("status") or {}
    return {
        "data": data,
        "source": src,
        "snapshot_built_at": (doc or {}).get("built_at"),
        "read_at": (doc or {}).get("read_at"),
        "status": status,
        "notice": NOTICE,
    }


def snap() -> dict[str, Any]:
    try:
        return source.snapshot()
    except Unavailable as err:
        raise HTTPException(503, str(err)) from err


def calls_with_integrity(calls: list[dict[str, Any]], doc: dict[str, Any]) -> list[dict[str, Any]]:
    status = doc["payload"].get("status") or {}
    now = now_iso()
    return [with_integrity(c, status, now) for c in calls]


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"ok": "serving"}


@app.get("/api/status")
def status() -> dict[str, Any]:
    doc = snap()
    return envelope(doc["payload"].get("status"), doc)


@app.get("/api/sources")
def sources() -> dict[str, Any]:
    doc = snap()
    return envelope(doc["payload"].get("sources", []), doc)


@app.get("/api/outlook")
def outlook() -> dict[str, Any]:
    doc = snap()
    rows = sorted(doc["payload"].get("outlook", []), key=lambda c: c.get("horizon_days") or 0)
    return envelope(calls_with_integrity(rows, doc), doc)


def _range(start: date | None, end: date | None) -> tuple[date, date]:
    end = end or datetime.now(UTC).date() + timedelta(days=7)
    start = start or end - MAX_RANGE
    if end < start or end - start > MAX_RANGE:
        raise HTTPException(422, "range must run forward and span at most 400 days")
    return start, end


@app.get("/api/calls")
def calls(
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    horizon: Annotated[int, Query(ge=0, le=6)] = 0,
) -> dict[str, Any]:
    doc = snap()
    lo, hi = _range(start, end)
    since = datetime.now(UTC).date() - MAX_RANGE
    if horizon not in SNAPSHOT_HORIZONS or lo < since:
        # The snapshot holds horizons 0 and 6 for 400 days; anything else is a live read, never an empty 200.
        try:
            rows = source.live(
                "calls", {"horizon": str(horizon), "start": lo.isoformat(), "end": hi.isoformat()}
            )
        except Unavailable as err:
            raise HTTPException(503, str(err)) from err
        return envelope(
            calls_with_integrity(sorted(rows, key=lambda c: c["target_date"]), doc), doc, "live"
        )
    hist = doc["payload"].get("history", [])
    rows = [
        c
        for c in hist
        if c.get("horizon_days") == horizon
        and lo.isoformat() <= (c.get("target_date") or "") <= hi.isoformat()
    ]
    last: dict[str, dict[str, Any]] = {}
    for c in rows:
        if c["target_date"] not in last or c["as_of"] > last[c["target_date"]]["as_of"]:
            last[c["target_date"]] = c
    return envelope(
        calls_with_integrity(sorted(last.values(), key=lambda c: c["target_date"]), doc), doc
    )


@app.get("/api/calls/{target_date}/evolution")
def evolution(target_date: date) -> dict[str, Any]:
    doc = snap()
    want = target_date.isoformat()
    rows = [c for c in doc["payload"].get("evidence", []) if c.get("target_date") == want]
    src = "snapshot"
    if not rows:
        try:
            rows, src = source.live("evolution", {"target_date": want}), "live"
        except Unavailable as err:
            raise HTTPException(503, str(err)) from err
    best: dict[Any, dict[str, Any]] = {}
    for c in rows:
        h = c.get("horizon_days")
        if h not in best or c["as_of"] > best[h]["as_of"]:
            best[h] = c
    out = sorted(best.values(), key=lambda c: -int(c.get("horizon_days") or 0))
    return envelope(calls_with_integrity(out, doc), doc, src)


@app.get("/api/calls/{as_of}/{target_date}")
def one_call(as_of: Annotated[str, PathParam(pattern=AS_OF)], target_date: date) -> dict[str, Any]:
    doc = snap()
    want = target_date.isoformat()
    rows = [
        c
        for c in doc["payload"].get("evidence", [])
        if c.get("as_of") == as_of and c.get("target_date") == want
    ]
    src = "snapshot"
    if not rows:
        try:
            rows, src = source.live("call", {"as_of": as_of, "target_date": want}), "live"
        except Unavailable as err:
            raise HTTPException(503, str(err)) from err
    if not rows:
        raise HTTPException(404, "no call for that as_of and date")
    traces = doc["payload"].get("traces", {})
    out = calls_with_integrity(rows, doc)
    for c in out:
        c["trace_summary"] = traces.get(c.get("trace_id") or "", None)
    return envelope(out, doc, src)


@app.get("/api/index")
def index(
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> dict[str, Any]:
    doc = snap()
    rows = doc["payload"].get("index", [])
    if start is None and end is None:
        return envelope(rows, doc)
    lo, hi = _range(start, end)
    have = [r for r in rows if lo.isoformat() <= r["date"] <= hi.isoformat()]
    if rows and rows[0]["date"] <= lo.isoformat():
        return envelope(have, doc)
    try:
        live = source.live("index", {"start": lo.isoformat(), "end": hi.isoformat()})
    except Unavailable as err:
        raise HTTPException(503, str(err)) from err
    return envelope(live, doc, "live")


_backtest: dict[str, Any] = {}


@app.get("/api/backtest")
def backtest() -> dict[str, Any]:
    """The newest backtest run, its measured result and the full-range index, live, kept 15 minutes."""
    doc = snap()
    if not _backtest or time.monotonic() - _backtest["at"] > BACKTEST_TTL_S:
        try:
            rows: dict[str, Any] = {
                "backtest": source.live("backtest", {}),
                "index": source.live("index_full", {}),
            }
        except Unavailable as err:
            raise HTTPException(503, str(err)) from err
        rows["measured"] = measure.summary(rows["backtest"])
        _backtest.update(at=time.monotonic(), data=rows)
    return envelope(_backtest["data"], doc, "live")


@app.get("/api/intraday")
def intraday() -> dict[str, Any]:
    """Hourly bars of recent sessions (display only) and the horizon-0 calls made in them."""
    doc = snap()
    got = doc["payload"].get("intraday") or {}
    return envelope({"bars": got.get("bars", []), "calls": got.get("calls", [])}, doc)


@app.get("/api/runs")
def runs(limit: Annotated[int, Query(ge=1, le=500)] = 48) -> dict[str, Any]:
    doc = snap()
    return envelope(doc["payload"].get("runs", [])[-limit:], doc)


@app.get("/")
def page() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")

if os.environ.get("WS_UI_LOG", "1") == "1":
    logging.basicConfig(level=logging.INFO)
