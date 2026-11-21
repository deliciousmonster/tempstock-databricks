"""The App in the main bundle on every target, no target-level run_as, the warehouse and the volume."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
APP_TARGETS = ("dev", "scratch", "prod")
APP_FILE = "resources/weather_signal_ui.app.yml"


def load(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def app(target: str) -> dict:
    return load(APP_FILE)["targets"][target]["resources"]["apps"]["ui_app"]


def test_every_target_carries_the_app_and_none_sets_a_target_run_as():
    """An App refuses a target-level run_as, so prod sets run_as per job and pipeline."""
    assert set(load(APP_FILE)["targets"]) == set(APP_TARGETS)
    targets = load("databricks.yml")["targets"]
    assert set(targets) == set(APP_TARGETS)
    assert all("run_as" not in targets[t] for t in APP_TARGETS)
    assert not (ROOT / "app" / "databricks.yml").exists()


@pytest.mark.parametrize("prefix", ["dev-", "t1-", "t999-"])
def test_app_name_is_valid(prefix):
    name = app("dev")["name"].replace("${var.app_prefix}", prefix)
    assert re.fullmatch(r"[a-z0-9-]{2,30}", name), name


@pytest.mark.parametrize("target", ["scratch", "prod"])
def test_every_target_deploys_the_same_app_as_dev(target):
    assert app(target) == app("dev")


def test_resources_are_the_warehouse_and_the_volume_read_only():
    res = app("dev")["resources"]
    assert [r["sql_warehouse"] for r in res if "sql_warehouse" in r] == [
        {"id": "${var.warehouse_id}", "permission": "CAN_USE"}
    ]
    assert [r["uc_securable"] for r in res if "uc_securable" in r] == [
        {
            "securable_full_name": "${resources.volumes.ui.catalog_name}."
            "${resources.volumes.ui.schema_name}.${resources.volumes.ui.name}",
            "securable_type": "VOLUME",
            "permission": "READ_VOLUME",
        }
    ]
