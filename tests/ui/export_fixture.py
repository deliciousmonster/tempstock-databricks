"""One snapshot for the export tests: an outlook made 10:30, checked by the 11:30 guard, read 12:15."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from tests.contract.snapshot_contract import CALL
from weather_signal.serving import snapshot

NOW = datetime(2026, 11, 18, 12, 15)
AS_OF = datetime(2026, 11, 18, 10, 30)
# Model holds by horizon, with signal.reasons as the model writes them.
HOLDS = {4: ["no_normal"], 5: ["forecast_quarantined", "stale_prices"], 6: []}
# Past horizon-0 calls on index days: (day offset, shown_action, signal_action).
PAST = [
    (5, "buy", "buy"),
    (10, None, "sell"),
    (15, "hold", "hold"),
    (20, "hold", "buy"),
    (25, None, "buy"),
]
INDEX = [
    {
        "date": date(2026, 10, 1) + timedelta(days=d),
        "close": 190.0 + d,
        "anomaly_c": (d % 9 - 4) / 2,
    }
    for d in range(48)
]


STATUS = {
    "open_incident": False,
    "post_verified_as_of": datetime(2026, 11, 18, 11, 30),
    "guarded_tables": 2,
    "read_at": NOW,
}


def _past(d: int, shown: str | None, signal: str) -> dict:
    c = {key: None for key in CALL} | {"est_price": None, "est_reason": "hold"}
    day = date(2026, 10, 1) + timedelta(days=d)
    c.update(
        as_of=datetime(day.year, day.month, day.day, 9, 30),
        target_date=day,
        horizon_days=0,
        signal_action=signal,
        shown_action=shown,
        signal_confidence=0.7,
        forecast_anomaly_c=1.0,
        source="agent" if shown else "signal_only",
    )
    return c


def intraday_bars() -> list[dict]:
    """Two synthetic sessions of hourly closes from 08:30, so the 10:30 outlook falls in the second."""
    out = []
    for day in (date(2026, 11, 17), date(2026, 11, 18)):
        for h in range(7):
            at = datetime(day.year, day.month, day.day, 8, 30) + timedelta(hours=h)
            out.append({"ticker": "WMT", "bar_start": at, "close": 200.0 + h / 4, "final": True})
    return out


def document(
    last_close: float | None = None,
    source_rows: list[dict] | None = None,
    est: dict[int, tuple[float | None, str | None]] | None = None,
) -> dict:
    calls = []
    for k in range(7):
        c = {key: None for key in CALL} | {"est_price": None, "est_reason": "hold"}
        c.update(
            as_of=AS_OF,
            target_date=AS_OF.date() + timedelta(days=k),
            horizon_days=k,
            signal_action="buy",
            shown_action="hold",
            source="agent",
            written_at=AS_OF + timedelta(minutes=11),
            rationale="<img src=x onerror=alert(1)>",
            last_close=last_close,
        )
        if k in HOLDS:
            c.update(signal_action="hold", reasons=HOLDS[k])
        if k < 2:
            c.update(
                shown_action=("buy", "sell")[k],
                signal_confidence=0.6 + k / 10,
                forecast_anomaly_c=1.5 - 3 * k,
            )
        c.update(zip(("est_price", "est_reason"), (est or {}).get(k, ()), strict=False))
        calls.append(c)
    past = [_past(d, shown, signal) for d, shown, signal in PAST]
    rows = {
        "v_ui_calls": calls + past,
        "v_ui_outlook": calls,
        "v_ui_status": [STATUS],
        "v_ui_index": INDEX,
        "v_ui_agent_runs": [],
        "v_ui_sources": source_rows or [],
        "v_ui_intraday": intraday_bars() if est is not None else [],
    }
    return snapshot.build(rows, {}, None, NOW)
