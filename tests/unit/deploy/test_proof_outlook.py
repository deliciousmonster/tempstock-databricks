"""The proof's end of the objective: normals, forecast, calls, the App over HTTP, the dashboard."""

from datetime import UTC, datetime

import pytest

from tests.unit.deploy.conftest import REPO
from tests.unit.deploy.proof_world import AS_OF, BEGAN, TARGETS, api_body
from tests.unit.deploy.proof_world import verified as _verify
from weather_signal.deploy import proof_outlook as po
from weather_signal.deploy.cli import Result, Unknown

NEW = (
    "normals period",
    "forecast for the current targets",
    "calls for today's targets",
    "each call carries its reason",
    "App serves the outlook",
    "dashboard",
)
OUTLOOK = "workspace.dev_app.v_ui_outlook"
URL = "https://dev-app.example/api/outlook"


def test_verify_proves_forecast_calls_ui_and_dashboard(p, ws):
    rc, checks = _verify(p, ws)
    assert rc == 0
    for name in NEW:
        assert checks[name]["ok"], checks[name]
    assert "7 of 7 targets" in checks["calls for today's targets"]["detail"]
    assert "no_champion 6" in checks["each call carries its reason"]["detail"]
    assert "14 of 14 datasets ran" in checks["dashboard"]["detail"]
    assert checks["normals period"]["detail"].startswith("1961-1990 366 of 366 days with a normal")


def test_the_bundle_p_hours_sets_the_expected_targets():
    assert po.p_hours(REPO) == 30
    want = po.expected_targets(int(AS_OF.timestamp()), 30)
    assert want == dict(TARGETS)


def _fails(p, ws, name):
    rc, checks = _verify(p, ws)
    assert rc == 1 and not checks[name]["ok"]
    return checks[name]["detail"]


def test_a_forecast_fetched_before_the_bootstrap_fails(p, ws):
    ws.rows["workspace.dev_raw.raw_envelopes"] = [{"s": int(BEGAN.timestamp()) - 1}]
    _fails(p, ws, "forecast for the current targets")


def test_a_target_without_its_forecast_index_row_fails_naming_it(p, ws):
    ws.rows["workspace.dev_signals.forecast_index"].pop(2)
    assert "none for 2026-11-20" in _fails(p, ws, "forecast for the current targets")


def test_a_missing_target_fails_and_the_app_check_too(p, ws):
    ws.rows[OUTLOOK].pop(3)
    assert "none for 2026-11-21" in _fails(p, ws, "calls for today's targets")
    assert not _verify(p, ws)[1]["App serves the outlook"]["ok"]


def test_an_outlook_scored_before_the_bootstrap_fails(p, ws):
    old = int(BEGAN.timestamp()) - 3600
    ws.rows[OUTLOOK] = [{**r, "as_of_s": old} for r in ws.rows[OUTLOOK]]
    assert "scored before" in _fails(p, ws, "calls for today's targets")


@pytest.mark.parametrize("rows", [[], "two-as-ofs"], ids=["empty", "two-as-ofs"])
def test_an_outlook_without_one_as_of_fails(p, ws, rows):
    if rows == "two-as-ofs":
        rows = [dict(r) for r in ws.rows[OUTLOOK]]
        rows[0]["as_of_s"] -= 3600
    ws.rows[OUTLOOK] = rows
    _fails(p, ws, "calls for today's targets")
    assert not _verify(p, ws)[1]["forecast for the current targets"]["ok"]


@pytest.mark.parametrize("change", [{"reasons": []}, {"reasons": None}, {"signal_action": None}])
def test_a_hold_without_its_reason_or_a_call_without_an_action_fails(p, ws, change):
    ws.rows[OUTLOOK][0].update(change)
    assert "bare 2026-11-18" in _fails(p, ws, "each call carries its reason")


@pytest.mark.parametrize(
    "answer",
    [(503, b'{"detail":"no snapshot has been read yet"}'), (302, b""), (401, b"{}"),
     (200, b"<html>login</html>"), (0, b"ConnectionError")],
    ids=["no-snapshot", "login-redirect", "unauthenticated", "html", "unreachable"],
)  # fmt: skip
def test_an_app_that_does_not_serve_the_outlook_fails(p, ws, answer):
    ws.http[URL] = answer
    assert _fails(p, ws, "App serves the outlook").startswith(f"GET /api/outlook: HTTP {answer[0]}")


def test_an_app_serving_another_as_of_or_action_fails(p, ws):
    stale = [dict(r) for r in ws.rows[OUTLOOK]]
    ws.http[URL] = (200, api_body(stale).replace(b"T12:30:00Z", b"T11:30:00Z"))
    assert "differ on" in _fails(p, ws, "App serves the outlook")
    stale[0]["signal_action"] = "buy"
    ws.http[URL] = (200, api_body(stale))
    assert "2026-11-18 buy" in _fails(p, ws, "App serves the outlook")


def test_the_app_check_waits_for_the_snapshot_that_holds_this_as_of(p, ws):
    answers = [(503, b""), (200, api_body(ws.rows[OUTLOOK]))]
    slept = []
    p.get, p.ctx.sleep = lambda url: answers.pop(0), slept.append
    assert po._ui(p, ws.rows[OUTLOOK], wait_s=600).ok
    assert slept == [po.POLL_S]
    p.get = lambda url: (503, b"")
    assert not po._ui(p, ws.rows[OUTLOOK], wait_s=120).ok


def test_an_unpublished_or_absent_dashboard_fails(p, ws):
    ws.rc["lakeview get-published"] = Result(1, "", "Error: Dashboard does not exist.\n")
    assert _fails(p, ws, "dashboard").startswith("not published")
    del ws.rc["lakeview get-published"]
    ws.dashboards = []
    assert _fails(p, ws, "dashboard").startswith("not found")


def test_a_dataset_that_errors_fails_and_each_runs_on_this_app_schema(p, ws):
    seen, real = [], p.ctx.sql

    def sql(q):
        seen.append(q)
        if "FROM (" in q and "v_ui_agent_runs" in q:
            raise Unknown("SQL did not answer: AnalysisException")
        return real(q)

    p.ctx.sql = sql
    detail = _fails(p, ws, "dashboard")
    assert "runs_daily: SQL did not answer" in detail and "11 of 14 datasets ran" in detail
    datasets = [q for q in seen if q.startswith("SELECT count(*) AS n FROM (")]
    assert datasets and all("workspace.dev_app.v_ui_" in q for q in datasets)
    assert not any(" v_ui_" in q or "(v_ui_" in q for q in datasets)


def _period(name, days, valued=None):
    return {"period": name, "days": days, "valued": days if valued is None else valued}


@pytest.mark.parametrize(
    "rows",
    [
        [_period("1998-2011 (training half)", 366)],
        [_period("1961-1990", 300), _period("1998-2011 (training half)", 66)],
        [_period("1961-1990", 365)],
        [_period("1961-1990", 366, valued=365)],
        [],
    ],
    ids=["training-half", "mixed", "a-day-short", "leap-day-null", "empty"],
)
def test_normals_that_fall_back_or_fall_short_fail_verify(p, ws, rows):
    ws.rows["workspace.dev_signals.normals"] = rows
    assert "want 1961-1990, 366 days with a normal" in _fails(p, ws, "normals period")


def test_a_late_error_fails_its_own_check_and_the_report_is_still_written(p, ws):
    ws.rc["lakeview get-published"] = Result(1, "", "Error: 403 PERMISSION_DENIED\n")
    order, real = [], p.get
    p.get = lambda url: order.append("get") or real(url)
    p.ctx.run = _recording(p.ctx.run, order)
    detail = _fails(p, ws, "dashboard")
    assert detail.startswith("did not answer: Unknown: lakeview get-published")
    assert order.index("get-published") < order.index("get")
    assert (p.out / "verify.md").is_file()


def _recording(run, order):
    def rec(argv, stdin=None):
        if list(argv[1:3]) == ["lakeview", "get-published"]:
            order.append("get-published")
        return run(argv, stdin)

    return rec


def test_an_outlook_that_does_not_answer_fails_its_check_and_the_report_is_written(p, ws):
    real = p.ctx.sql

    def sql(q):
        if "v_ui_outlook" in q:
            raise Unknown("SQL did not answer: TimeoutError")
        return real(q)

    p.ctx.sql = sql
    assert "TimeoutError" in _fails(p, ws, "calls for today's targets")


@pytest.mark.parametrize("status", [302, 401, 403, po.AUTH_ERROR])
def test_an_app_answer_that_does_not_change_fails_after_three_polls(p, ws, status):
    slept = []
    p.get, p.ctx.sleep = (lambda url: (status, b"")), slept.append
    assert not po._ui(p, ws.rows[OUTLOOK], wait_s=4500).ok
    assert slept == [po.POLL_S, po.POLL_S]


def test_a_redirect_that_clears_on_the_third_poll_passes(p, ws):
    answers = [(302, b""), (302, b""), (200, api_body(ws.rows[OUTLOOK]))]
    slept = []
    p.get, p.ctx.sleep = (lambda url: answers.pop(0)), slept.append
    assert po._ui(p, ws.rows[OUTLOOK], wait_s=4500).ok
    assert slept == [po.POLL_S, po.POLL_S]


def test_a_redirect_between_other_answers_restarts_the_count(p, ws):
    answers = [(302, b""), (302, b""), (503, b""), (302, b""), (302, b""), (302, b"")]
    slept = []
    p.get, p.ctx.sleep = (lambda url: answers.pop(0)), slept.append
    assert not po._ui(p, ws.rows[OUTLOOK], wait_s=4500).ok
    assert len(slept) == 5 and answers == []


def test_an_authenticate_error_is_an_answer_the_poll_retries(monkeypatch):
    import databricks.sdk.core as core

    class Broken:
        def authenticate(self):
            raise ValueError("token expired")

    monkeypatch.setattr(core, "Config", lambda **kw: Broken())
    assert po.sdk_get("p")("http://127.0.0.1:9/api/outlook") == (po.AUTH_ERROR, b"ValueError")


def test_an_empty_outlook_fails_the_app_check_without_waiting(p, ws):
    slept = []
    p.ctx.sleep = slept.append
    assert not po._ui(p, [], wait_s=4500).ok
    assert slept == []


def test_the_sdk_config_is_built_on_the_first_get_only(monkeypatch):
    import databricks.sdk.core as core

    built = []
    monkeypatch.setattr(core, "Config", lambda **kw: built.append(kw) or _NoAuth())
    get = po.sdk_get("p")
    assert built == []
    get("http://127.0.0.1:9/api/outlook")
    get("http://127.0.0.1:9/api/outlook")
    assert built == [{"profile": "p"}]


class _NoAuth:
    def authenticate(self):
        return {}


def test_an_as_of_at_half_past_midnight_utc_expects_six_targets():
    at = int(datetime(2026, 11, 21, 0, 30, tzinfo=UTC).timestamp())
    want = po.expected_targets(at, 30)
    assert want == {f"2026-11-{20 + k}": k for k in range(1, 7)}
    rows = [{"as_of_s": at, "target_date": t, "signal_action": "hold",
             "reasons": ["no_champion"]} for t in want]  # fmt: skip
    began = at - 3600
    (calls, reasons), got = po.calls_checks(rows, began, 30)
    assert calls.ok and reasons.ok and got == want and "6 of 6 targets" in calls.detail
