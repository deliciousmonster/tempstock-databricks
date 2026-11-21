"""The App principal's grants live in the main bundle per App target, by reference to the App."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
RUNNER = "${var.runner}"
APP_SP = "${resources.apps.ui_app.service_principal_client_id}"


def load(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def test_signals_and_ui_grants_come_from_the_variables():
    assert (
        load("resources/catalog.yml")["resources"]["schemas"]["signals"]["grants"]
        == "${var.signals_grants}"
    )
    assert (
        load("resources/catalog.yml")["resources"]["volumes"]["ui"]["grants"]
        == "${var.ui_volume_grants}"
    )


def test_the_default_is_the_runner_alone():
    variables = load("databricks.yml")["variables"]
    for var in ("signals_grants", "ui_volume_grants"):
        assert [g["principal"] for g in variables[var]["default"]] == [RUNNER]


def test_every_app_target_grants_its_own_app_and_keeps_the_runner():
    """An App the main bundle omits loses its grants on the next deploy."""
    default = load("databricks.yml")["variables"]
    for target, body in load("resources/weather_signal_ui.app.yml")["targets"].items():
        for var, priv in (
            ("signals_grants", ["USE_SCHEMA"]),
            ("ui_volume_grants", ["READ_VOLUME"]),
        ):
            got = body["variables"][var]
            assert got[0] == default[var]["default"][0], target
            assert {"principal": APP_SP, "privileges": priv} in got, target
