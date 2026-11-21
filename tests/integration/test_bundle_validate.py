"""`bundle validate` passes for both targets; `dev` schedules are live unless bootstrap pauses them."""

import json

import pytest

from tests.integration.conftest import cli


@pytest.mark.parametrize("target", ["dev", "prod"])
def test_validate(target):
    r = cli("bundle", "validate", "-t", target)
    assert r.returncode == 0, r.stderr[-800:]
    assert "Validation OK" in r.stdout + r.stderr


@pytest.mark.parametrize(
    ("extra", "status"), [((), "UNPAUSED"), (("--var", "schedule_pause=PAUSED"), "PAUSED")]
)
def test_dev_schedules_follow_the_pause_variable(extra, status):
    r = cli("bundle", "validate", "-t", "dev", *extra, "-o", "json")
    assert r.returncode == 0, r.stderr[-800:]
    jobs = json.loads(r.stdout)["resources"]["jobs"]
    scheduled = [j for j in jobs.values() if "schedule" in j]
    assert scheduled and all(j["schedule"]["pause_status"] == status for j in scheduled)
    assert all(j["name"].startswith("dev_") for j in jobs.values())
