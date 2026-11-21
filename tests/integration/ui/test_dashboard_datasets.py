"""Each dataset of the deployed dashboard runs with its own catalog and schema, as the dashboard runs it."""

from __future__ import annotations

import json

import pytest

from tests.integration.conftest import PROFILE, cli, warehouse_id


@pytest.fixture(scope="module")
def deployed():
    out = cli("bundle", "summary", "-t", "dev", "-o", "json", timeout=120)
    assert out.returncode == 0, out.stderr
    dash_id = json.loads(out.stdout)["resources"]["dashboards"]["ui_dashboard"]["id"]
    got = cli("lakeview", "get", dash_id, "-o", "json", timeout=120)
    assert got.returncode == 0, got.stderr
    return json.loads(json.loads(got.stdout)["serialized_dashboard"])


def test_datasets_carry_the_dev_schema(deployed):
    assert {(d.get("catalog"), d.get("schema")) for d in deployed["datasets"]} == {
        ("workspace", "dev_signals")
    }


def test_every_dataset_runs(deployed):
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.sql import ExecuteStatementRequestOnWaitTimeout, StatementState

    w = WorkspaceClient(profile=PROFILE)
    failed = {}
    for d in deployed["datasets"]:
        r = w.statement_execution.execute_statement(
            statement=" ".join(d["queryLines"]),
            warehouse_id=warehouse_id(),
            catalog=d["catalog"],
            schema=d["schema"],
            wait_timeout="50s",
            on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CANCEL,
        )
        if r.status.state != StatementState.SUCCEEDED:
            failed[d["name"]] = (r.status.state, r.status.error and r.status.error.message)
    assert not failed
