"""`params`: no output field is an action; a date with no proposal is a downgrade naming why."""

import dataclasses

import pytest

from weather_signal.agent.proposal import MergeParams, Proposal, fill, params, template


def test_no_field_is_an_action():
    names = {f.name for f in dataclasses.fields(MergeParams)}
    assert names == {"target_date", "downgrade", "downgrade_reason", "rationale"}
    assert not names & {"action", "signal_action"}


@pytest.mark.parametrize(
    "p",
    [
        Proposal("2026-11-18", False, None, "x" * 40),
        Proposal("2026-11-18", True, "stale_inputs", "y" * 40),
    ],
)
def test_proposal_passes_through(p):
    out = params("2026-11-18", p, missing="llm_incomplete")
    assert (out.downgrade, out.downgrade_reason, out.rationale) == (
        p.downgrade,
        p.reason,
        p.rationale,
    )


@pytest.mark.parametrize("missing", ["llm_unavailable", "llm_incomplete"])
def test_none_is_a_downgrade_naming_missing(missing):
    out = params("2026-11-18", None, missing=missing)
    assert out.downgrade is True
    assert out.downgrade_reason == missing
    assert missing in out.rationale


def test_template_refuses_a_model_reason():
    with pytest.raises(ValueError, match="not a code reason"):
        template("stale_inputs")


def test_fill_binds_every_date():
    p = Proposal("a", False, None, "z" * 40)
    out = fill(["a", "b"], {"a": p}, missing="llm_incomplete")
    assert out["a"].downgrade is False
    assert out["b"] == MergeParams("b", True, "llm_incomplete", template("llm_incomplete"))


def test_a_proposal_for_another_date_is_never_bound():
    """2026-11-19 11:30Z: one date's row carried another date's text; the pairing now refuses it."""
    other = Proposal("2026-11-25", False, None, "z" * 40)
    with pytest.raises(ValueError, match="2026-11-25 offered as 2026-11-24"):
        fill(["2026-11-24"], {"2026-11-24": other}, missing="llm_incomplete")
