"""Intraday is deployed, proved and torn down with the rest: a task, a pipeline table and a view."""

from __future__ import annotations

import tomllib

import yaml

from weather_signal import config
from weather_signal.deploy import phases
from weather_signal.deploy import proof_checks as pc
from weather_signal.ingest import intraday
from weather_signal.serving import views

from .conftest import REPO


def _ingest() -> dict:
    doc = yaml.safe_load((REPO / "resources/ingest.job.yml").read_text(encoding="utf-8"))
    return doc["resources"]["jobs"]["ingest"]


def test_intraday_is_its_own_task_of_the_hourly_ingest_job_and_waits_on_nothing():
    job = _ingest()
    tasks = {t["task_key"]: t for t in job["tasks"]}
    task = tasks["intraday"]
    assert "depends_on" not in task and "depends_on" not in tasks["ingest"]
    assert task["python_wheel_task"]["entry_point"] == "weather-signal-intraday"
    assert task["timeout_seconds"] > 0
    assert job["schedule"]["quartz_cron_expression"] == "0 5 * * * ?"
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    scripts = project["scripts"]
    assert scripts["weather-signal-intraday"] == "weather_signal.jobs.intraday:main"


def test_prod_runs_the_task_as_the_runner_like_the_other_ingest_work():
    doc = yaml.safe_load((REPO / "databricks.yml").read_text(encoding="utf-8"))
    run_as = doc["targets"]["prod"]["resources"]["jobs"]["ingest"]["run_as"]
    assert run_as == {"service_principal_name": "${var.runner}"}


def test_the_proof_counts_the_table_and_accepts_it_empty_but_not_absent():
    assert ("daily", "prices_intraday") in phases.pipeline_tables(REPO)
    t = "workspace.dev_daily.prices_intraday"
    assert pc.table_checks({t: 0})[0].ok
    assert not pc.table_checks({t: None})[0].ok


def test_everything_it_writes_sits_in_a_bundle_schema_or_volume_that_teardown_removes():
    n = config.Names("workspace", "t1_")
    landed = f"{n.raw}/{intraday.SOURCE}/"
    assert landed.startswith("/Volumes/workspace/t1_raw/raw/")
    assert "t1_daily.prices_intraday" in views.intraday(n)
    statements = views.statements(n)
    assert f"CREATE OR REPLACE VIEW workspace.t1_app.{views.INTRADAY_VIEW} AS " in "\n".join(
        statements
    )
    catalog = yaml.safe_load((REPO / "resources/catalog.yml").read_text(encoding="utf-8"))
    assert {"raw", "daily", "app"} <= set(catalog["resources"]["schemas"])
    assert catalog["resources"]["volumes"]["raw_files"]["name"] == "raw"
