"""`--destroy` takes a throwaway tN_ scope alone, refused before any call, and reports residue."""

import pytest

from weather_signal.deploy import destroy, scope
from weather_signal.deploy.cli import Stop


@pytest.mark.parametrize(
    ("target", "prefix", "yes"),
    [
        ("dev", "dev_", True),
        ("prod", "", True),
        ("scratch", "dev_", True),
        ("scratch", "", True),
        ("scratch", None, True),
        ("scratch", "t1_", False),
    ],
)
def test_refused_before_any_call(target, prefix, yes):
    with pytest.raises(Stop) as e:
        destroy.refuse_unless_scratch(target, prefix, yes)
    assert e.value.code == 2


def test_cli_refuses_dev_destroy_before_building_anything(monkeypatch, capsys):
    from weather_signal.cli import main

    monkeypatch.setattr("weather_signal.deploy.command.real_ctx", lambda *a: pytest.fail("built"))
    assert main(["bootstrap", "--profile", "p", "--target", "dev", "--destroy", "--yes"]) == 2
    assert "--destroy takes only --target scratch" in capsys.readouterr().out


def test_residue_is_listed_and_exit_1(ctx, ws):
    ctx.scope = scope.build("p", "scratch", "t1_", "workspace")
    ws.schemas = ["t1_signals", "dev_signals"]
    assert destroy.destroy(ctx) == 1
    assert ws.writes()[0][1:3] == ["bundle", "destroy"] and "--auto-approve" in ws.writes()[0]
    assert "destroy: still present: schema t1_signals" in ctx.lines


def test_nothing_left_is_exit_0(ctx, ws):
    ctx.scope = scope.build("p", "scratch", "t1_", "workspace")
    assert destroy.destroy(ctx) == 0
