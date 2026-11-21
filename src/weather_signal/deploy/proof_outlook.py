"""The proof's end of the objective: the forecast fetched for the current targets, a call per
target, the App serving those calls over HTTP, and the dashboard published with datasets that run."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from weather_signal import asof
from weather_signal.deploy.cli import Unknown, not_found
from weather_signal.deploy.proof_checks import Check, failed, guarded
from weather_signal.signal.model import HORIZON_CAP

if TYPE_CHECKING:
    from weather_signal.deploy.proof import Proof

Get = Callable[[str], tuple[int, bytes]]  # url -> (HTTP status, body), with the workspace's auth
FORECAST_SOURCE = "open-meteo-previous-runs"
ACTIONS = ("buy", "sell", "hold")
P_HOURS = re.compile(r"^  p_hours:\n(?:    .*\n)*?    default: \"?(\d+)\"?", re.MULTILINE)
VIEW = re.compile(r"(?<![\w.])(v_ui_\w+)")
POLL_S = 60.0
AUTH_ERROR = -1  # the status sdk_get returns when the SDK config or authenticate() raised
# Login redirect, refused auth, no token: final after FINAL_POLLS in a row, not on the first.
FINAL, FINAL_POLLS = (302, 401, 403, AUTH_ERROR), 3
FORECAST, UI = "forecast for the current targets", "App serves the outlook"


def p_hours(repo: Path) -> int:
    """The bundle's `p_hours` default, as the score job is deployed with it."""
    m = P_HOURS.search((repo / "databricks.yml").read_text(encoding="utf-8"))
    if m is None:
        raise Unknown("databricks.yml has no p_hours default")
    return int(m.group(1))


def expected_targets(as_of_s: int, p: int) -> dict[str, int]:
    """target date -> horizon, every target the score writes at this as_of."""
    at = datetime.fromtimestamp(as_of_s, UTC)
    day = asof.decision_day(at, p)
    return {asof.target_date(day, k).isoformat(): k for k in asof.horizons(at, p)}


def _at(s: int | None) -> str:
    return "none" if s is None else datetime.fromtimestamp(s, UTC).isoformat()


def forecast_check(
    fetched_s: int | None, began_s: int, index: set[tuple[str, int]], want: Mapping[str, int]
) -> Check:
    """A forecast envelope fetched since the bootstrap began, and an index row visible at the
    as_of at each scored target's fitted lead."""
    scored = {t: k for t, k in want.items() if k < HORIZON_CAP}
    missing = sorted(t for t, k in scored.items() if (t, asof.fitted_lead(k)) not in index)
    fresh = fetched_s is not None and fetched_s >= began_s
    ok = fresh and bool(scored) and not missing
    detail = (
        f"newest forecast envelope fetched {_at(fetched_s)} (bootstrap began {_at(began_s)}); "
        f"index rows for {len(scored) - len(missing)} of {len(scored)} targets"
        + (f"; none for {', '.join(missing)}" if missing else "")
    )
    return Check("forecast for the current targets", ok, detail)


def calls_checks(
    rows: Sequence[Mapping[str, Any]], began_s: int, p: int
) -> tuple[list[Check], dict[str, int]]:
    """v_ui_outlook: one call per target the horizon expects at an as_of scored in this run,
    each a known action and each hold with its reason. Also returns the expected targets."""
    as_ofs = {int(r["as_of_s"]) for r in rows}
    if len(as_ofs) != 1:
        why = f"{len(rows)} outlook rows across {len(as_ofs)} as_of values"
        return [Check("calls for today's targets", False, why)], {}
    (as_of_s,) = as_ofs
    want = expected_targets(as_of_s, p)
    have = Counter(str(r["target_date"]) for r in rows)
    missing = sorted(set(want) - set(have))
    extra = sorted(t for t, n in have.items() if t not in want or n > 1)
    ok = as_of_s >= began_s and not missing and not extra
    detail = f"as_of {_at(as_of_s)}: {len(have)} of {len(want)} targets" + "".join(
        f"; {label} {', '.join(ds)}"
        for label, ds in (("none for", missing), ("extra", extra))
        if ds
    )
    if as_of_s < began_s:
        detail += f"; scored before the bootstrap began {_at(began_s)}"
    return [Check("calls for today's targets", ok, detail), _reasons(rows)], want


def _reasons(rows: Sequence[Mapping[str, Any]]) -> Check:
    bad = sorted(
        str(r["target_date"])
        for r in rows
        if r.get("signal_action") not in ACTIONS
        or (r.get("signal_action") == "hold" and not list(r.get("reasons") or []))
    )
    tally = Counter(
        str(x) for r in rows if r.get("signal_action") == "hold" for x in r["reasons"] or []
    )
    seen = ", ".join(f"{k} {n}" for k, n in sorted(tally.items())) or "no hold reasons"
    detail = f"{sum(r.get('signal_action') == 'hold' for r in rows)} holds ({seen})"
    return Check(
        "each call carries its reason",
        not bad,
        detail + (f"; bare {', '.join(bad)}" if bad else ""),
    )


def _epoch(v: Any) -> int | None:
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def ui_check(status: int, body: bytes, rows: Sequence[Mapping[str, Any]]) -> Check:
    """/api/outlook answers 200 with the same as_of, targets and signal actions as v_ui_outlook."""
    name = "App serves the outlook"
    try:
        data = json.loads(body)["data"] if status == 200 else None
    except (ValueError, KeyError, TypeError):
        data = None
    if not isinstance(data, list):
        return Check(name, False, f"GET /api/outlook: HTTP {status}, no data list")
    api = {
        (_epoch(c.get("as_of")), str(c.get("target_date")), c.get("signal_action")) for c in data
    }
    sql = {(int(r["as_of_s"]), str(r["target_date"]), r.get("signal_action")) for r in rows}
    ok = bool(sql) and api == sql and len(data) == len(rows)
    detail = f"HTTP 200, {len(data)} calls; v_ui_outlook {len(rows)}"
    diff = sorted(f"{t} {a}" for _, t, a in api ^ sql)
    return Check(name, ok, detail + (f"; differ on {', '.join(diff)}" if diff else ""))


def dashboard_check(published: bool | None, errors: Sequence[str], n: int) -> Check:
    state = {True: "published", False: "not published", None: "not found"}[published]
    ok = published is True and not errors
    detail = f"{state}; {n - len(errors)} of {n} datasets ran"
    return Check("dashboard", ok, detail + (f"; {'; '.join(errors)}" if errors else ""))


# Reads: the workspace through the proof's context, and the App over HTTP.


def _outlook(p: Proof) -> list[dict[str, Any]]:
    view = f"{p.ctx.scope.names.schema('app')}.v_ui_outlook"
    return p.ctx.sql(
        "SELECT CAST(unix_timestamp(as_of) AS BIGINT) AS as_of_s, "
        f"CAST(target_date AS STRING) AS target_date, signal_action, reasons FROM {view}"
    )


def _forecast(p: Proof, began_s: int, as_of_s: int, want: Mapping[str, int]) -> Check:
    n = p.ctx.scope.names
    env = p.ctx.sql(
        "SELECT CAST(unix_timestamp(max(fetched_at)) AS BIGINT) AS s "
        f"FROM {n.table('raw', 'raw_envelopes')} WHERE source = '{FORECAST_SOURCE}'"
    )
    fetched = env[0]["s"] if env else None
    dates = ", ".join(f"DATE'{date.fromisoformat(t).isoformat()}'" for t in want) or "NULL"
    idx = p.ctx.sql(
        "SELECT CAST(target_date AS STRING) AS target_date, lead_days "
        f"FROM {n.table('signals', 'forecast_index')} WHERE target_date IN ({dates}) "
        f"AND known_at <= timestamp_seconds({int(as_of_s)})"
    )
    index = {(str(r["target_date"]), int(r["lead_days"])) for r in idx}
    return forecast_check(None if fetched is None else int(fetched), began_s, index, want)


def _ui(p: Proof, rows: Sequence[Mapping[str, Any]], wait_s: float) -> Check:
    """Polls until the App's snapshot holds this as_of: it is rebuilt hourly at :15."""
    url = str((p.ctx.app() or {}).get("url") or "").rstrip("/")
    if not url or p.get is None:
        return Check(UI, False, "no App URL" if not url else "no HTTP client")
    waited, final = 0.0, 0
    while True:
        status, body = p.get(f"{url}/api/outlook")
        check = ui_check(status, body, rows)
        final = final + 1 if status in FINAL else 0
        if check.ok or waited >= wait_s or final >= FINAL_POLLS or not rows:
            return check
        p.ctx.sleep(POLL_S)
        waited += POLL_S


def _dashboard(p: Proof) -> Check:
    s = p.ctx.scope
    doc = json.loads((p.ctx.repo / "aibi/weather_signal.lvdash.json").read_text(encoding="utf-8"))
    sets = doc.get("datasets") or []
    found = [
        d
        for d in p.ctx.cli("lakeview", "list") or []
        if d.get("display_name") == f"{s.prefix}weather-signal"
    ]
    published: bool | None = None
    if len(found) == 1:
        argv = ["databricks", "lakeview", "get-published", str(found[0]["dashboard_id"])]
        r = p.ctx.run([*argv, "--profile", s.profile, "-o", "json"])
        if r.rc != 0 and not not_found(r):
            raise Unknown(f"lakeview get-published: exit {r.rc}: {r.tail(3)}")
        published = r.rc == 0
    app = s.names.schema("app")
    errors = []
    for d in sets:
        q = VIEW.sub(rf"{app}.\1", "".join(d.get("queryLines") or []))
        try:
            p.ctx.sql(f"SELECT count(*) AS n FROM ({q})")
        except Unknown as err:
            errors.append(f"{d.get('name')}: {str(err)[:120]}")
    return dashboard_check(published, errors, len(sets))


def checks(p: Proof, began: datetime, wait_s: float) -> list[Check]:
    """The dashboard first and the App's poll last; an error fails its own check only."""
    began_s = int(began.timestamp())
    out = guarded("dashboard", lambda: [_dashboard(p)])
    try:
        rows = _outlook(p)
        calls, want = calls_checks(rows, began_s, p_hours(p.ctx.repo))
    except Exception as err:  # noqa: BLE001 - a failed check, so the report is still written
        return [*out, failed("calls for today's targets", err)]
    if want:
        as_of_s = int(rows[0]["as_of_s"])
        fc = guarded(FORECAST, lambda: [_forecast(p, began_s, as_of_s, want)])
    else:
        fc = [forecast_check(None, began_s, set(), {})]
    return [*out, *fc, *calls, *guarded(UI, lambda: [_ui(p, rows, wait_s)])]


def sdk_get(profile: str) -> Get:
    """GET with the profile's own workspace auth; a redirect (the login page) is not followed.
    The SDK config is built on the first GET: building it reads the host over the network."""
    import requests

    cfg: list[Any] = []

    def get(url: str) -> tuple[int, bytes]:
        try:
            if not cfg:
                from databricks.sdk.core import Config

                cfg.append(Config(profile=profile))
            headers = dict(cfg[0].authenticate())
        except Exception as err:  # noqa: BLE001 - the poll retries it, then fails the check
            return AUTH_ERROR, type(err).__name__.encode()
        try:
            r = requests.get(url, headers=headers, timeout=(30, 60), allow_redirects=False)
        except (requests.RequestException, OSError) as err:
            return 0, type(err).__name__.encode()
        return r.status_code, r.content

    return get
