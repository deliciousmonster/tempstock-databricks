"""verify's App log check: a crash after the active deployment's startup fails the leg, not only a
shutdown line. The crash lines are expected shapes, not ones seen on dev."""

import pytest

from tests.unit.deploy.proof_world import APP_LOG
from weather_signal.deploy import proof_checks as pc

TRACEBACK = """\
2026-11-19T22:50:00Z [APP] Traceback (most recent call last):
2026-11-19T22:50:00Z [APP]   File "/app/python/source_code/ws_ui/main.py", line 1, in <module>
2026-11-19T22:50:00Z [APP] RuntimeError: boom
"""


@pytest.mark.parametrize(
    "tail",
    [
        TRACEBACK,
        "2026-11-19T22:50:01Z [SYSTEM] App process exited with code 1\n",
        "2026-11-19T22:50:01Z [SYSTEM] Process exited with status 137\n",
    ],
    ids=["traceback", "exit-code", "exit-status"],
)
def test_a_crash_after_startup_fails_the_app_log_check(tail):
    check = pc.app_log_check(APP_LOG + tail, "dep-2")
    assert not check.ok
    assert "dep-2 crashed (2026-11-19T22:50:0" in check.detail


def test_a_clean_exit_line_after_startup_still_passes():
    log = APP_LOG + "2026-11-19T22:50:01Z [SYSTEM] build step exited with code 0\n"
    assert pc.app_log_check(log, "dep-2").ok


def test_a_traceback_from_an_older_deployment_does_not_fail_the_active_one():
    old, new = APP_LOG.split("2026-11-19T21:23:52Z")
    assert pc.app_log_check(old + TRACEBACK + "2026-11-19T21:23:52Z" + new, "dep-2").ok


def test_a_handled_500_traceback_does_not_fail_a_serving_app():
    """One request's traceback failed an app that was still serving."""
    tail = (
        "2026-11-19T22:50:00Z [APP] ERROR:    Exception in ASGI application\n"
        + TRACEBACK
        + '2026-11-19T22:50:01Z [APP] INFO:     192.0.2.1 - "GET /x HTTP/1.1" 500\n'
    )
    assert pc.app_log_check(APP_LOG + tail, "dep-2").ok


def test_exit_and_status_in_ordinary_text_is_not_an_exit():
    tail = "2026-11-19T22:50:00Z [APP] INFO: position exit signal, status 3 open\n"
    assert pc.app_log_check(APP_LOG + tail, "dep-2").ok


def test_a_crash_followed_by_a_restart_that_came_up_passes():
    tail = TRACEBACK + "2026-11-19T22:51:00Z [APP] INFO:     Application startup complete.\n"
    assert pc.app_log_check(APP_LOG + tail, "dep-2").ok


CHAINED = """\
2026-11-19T22:50:00Z [APP]
2026-11-19T22:50:00Z [APP] During handling of the above exception, another exception occurred:
2026-11-19T22:50:00Z [APP]
2026-11-19T22:50:00Z [APP] Traceback (most recent call last):
2026-11-19T22:50:00Z [APP] ValueError: bad
"""


def test_a_handled_500_with_a_chained_traceback_does_not_fail_a_serving_app():
    """The chained traceback after a blank line read as a crash."""
    tail = (
        "2026-11-19T22:50:00Z [APP] ERROR:    Exception in ASGI application\n"
        + TRACEBACK
        + CHAINED
        + '2026-11-19T22:50:01Z [APP] INFO:     192.0.2.1 - "GET /x HTTP/1.1" 500\n'
    )
    assert pc.app_log_check(APP_LOG + tail, "dep-2").ok


def test_a_chained_traceback_after_an_unhandled_one_still_fails():
    check = pc.app_log_check(APP_LOG + TRACEBACK + CHAINED, "dep-2")
    assert not check.ok and "dep-2 crashed" in check.detail
