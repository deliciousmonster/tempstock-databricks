"""A Stop or Unknown from a CLI call carries the CLI's own error text."""

from weather_signal.deploy.cli import Result, Stop


def test_a_stop_keeps_the_error_line_that_scrolled_above_the_progress_lines():
    """The deploy's 403 sat above eight `Created ...` lines and the Stop showed only those."""
    err = "Error: cannot create resources.genie_spaces.ui_genie: 403\n" + "Created x\n" * 12
    tail = Result(1, "Files: 0 uploaded\n", err).tail()
    assert "Error: cannot create resources.genie_spaces.ui_genie: 403" in tail
    assert "Files: 0 uploaded" in tail and tail.count("Created x") == 8


def test_a_failed_bundle_run_reports_stderr(ctx, ws):
    ws.rc["bundle run"] = Result(1, "Run URL: u\n", "Error: run failed: boom\n" + "Waiting\n" * 9)
    try:
        ctx.bundle_run("ingest")
        raise AssertionError("no Stop")
    except Stop as err:
        assert "Error: run failed: boom" in str(err)
