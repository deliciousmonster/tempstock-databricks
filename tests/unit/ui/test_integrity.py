"""U2 in Python: every case of the shared fixture, run on the App's module."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("ws_integrity", ROOT / "app/ws_ui/integrity.py")
assert _spec and _spec.loader
integrity = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(integrity)
FIXTURE = json.loads((ROOT / "tests/fixtures/integrity_cases.json").read_text(encoding="utf-8"))


def case_inputs(case: dict) -> tuple[dict, object]:
    row = {**FIXTURE["base"], **case["with"]}
    now = row["read_at"] if case.get("now_is_read_at") else case.get("now", FIXTURE["now"])
    return row, now


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=[c["name"] for c in FIXTURE["cases"]])
def test_fixture_case(case):
    row, now = case_inputs(case)
    assert integrity.state(row, now) == case["want"]


@pytest.mark.parametrize(
    "value", ["2026-11-17T08:15:00+19:00", "2026-11-17T08:15:00+02:60", "0000-01-01T00:00:00Z"]
)
def test_review_r4_m2_timestamps_are_unreadable(value):
    with pytest.raises(integrity.Unreadable):
        integrity.ts(value)


def test_every_state_has_a_label():
    states = {c["want"] for c in FIXTURE["cases"]}
    assert states <= set(integrity.LABELS)
    assert integrity.label("stale", "08:15") == "unknown: integrity as of 08:15"
