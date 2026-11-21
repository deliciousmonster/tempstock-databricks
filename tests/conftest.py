from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SEED = Path(__file__).parent.parent / "src" / "weather_signal" / "seed"


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


def fixture_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture(scope="session")
def cal():
    from weather_signal import sessions

    return sessions.load(SEED / "sessions.csv")
