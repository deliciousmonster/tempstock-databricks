"""A fake catalog set up the way `setup` leaves it: both guarded tables, guard rows and acts."""

from __future__ import annotations

from tests.unit.fake_catalog import FakeCatalog
from weather_signal.catalog import setup
from weather_signal.catalog.ddl import specs
from weather_signal.config import Names

N = Names("workspace", "dev_")
SIG, RECS = N.guarded
SPECS = specs(N)


def world() -> FakeCatalog:
    cat = FakeCatalog()
    setup.run(cat, N)
    return cat


def signal_row(i: int, action: str = "buy", **kw) -> dict:
    return {
        "as_of": f"2026-12-02T{i:02d}:30",
        "target_date": "2026-12-03",
        "action": action,
        "confidence": 0.5,
        **kw,
    }


def rec_row(i: int, action: str = "buy", signal_action: str = "buy", **kw) -> dict:
    return {
        "as_of": f"2026-12-02T{i:02d}:30",
        "target_date": "2026-12-03",
        "action": action,
        "signal_action": signal_action,
        "confidence": 0.5,
        **kw,
    }
