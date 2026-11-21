"""The scope: dev, a throwaway tN_ scratch prefix or unprefixed prod, never a default profile."""

import pytest

from weather_signal.deploy import scope


def test_dev_is_dev_prefixed():
    s = scope.build("p", "dev", None, "workspace")
    assert (s.prefix, s.app_prefix, s.app_name) == ("dev_", "dev-", "dev-weather-signal-ui")
    assert s.raw_volume == "/Volumes/workspace/dev_raw/raw"


@pytest.mark.parametrize(
    ("profile", "target", "prefix", "catalog"),
    [
        (None, "dev", None, "workspace"),
        ("p", "accept", "acc_", "workspace"),
        ("p", "prod", "dev_", "workspace"),
        ("p", "prod", "t1_", "workspace"),
        ("p", "accept", "t1_", "workspace"),
        ("p", "dev", "t1_", "workspace"),
        ("p", "dev", "", "workspace"),
        ("p", "scratch", None, "workspace"),
        ("p", "scratch", "", "workspace"),
        ("p", "scratch", "dev_", "workspace"),
        ("p", "scratch", "t1234_", "workspace"),
        ("p", "scratch", "t1_x", "workspace"),
        ("p", "scratch", "t1_", "Work-space"),
        ("p", "scratch", "t1_", "a;DROP"),
    ],
)
def test_refused_scopes(profile, target, prefix, catalog):
    with pytest.raises(scope.ScopeError):
        scope.build(profile, target, prefix, catalog)


def test_prod_is_unprefixed_and_its_deploy_args_carry_the_pause_status():
    s = scope.build("p", "prod", None, "workspace")
    assert (s.prefix, s.app_name) == ("", "weather-signal-ui")
    args = s.bundle_args("wh-1")
    assert args[args.index("-t") + 1] == "prod" and "schedule_pause=PAUSED" in args
    assert "schedule_pause=UNPAUSED" in s.bundle_args("wh-1", "UNPAUSED")


def test_scratch_args_carry_every_variable_the_bundle_lacks_a_default_for():
    s = scope.build("p", "scratch", "t12_", "workspace")
    args = s.bundle_args("wh-1")
    assert args[:4] == ["--profile", "p", "-t", "scratch"]
    assert {"schema_prefix=t12_", "app_prefix=t12-", "warehouse_id=wh-1"} <= set(args)
    assert s.fq("signals", "signal") == "`workspace`.`t12_signals`.`signal`"
