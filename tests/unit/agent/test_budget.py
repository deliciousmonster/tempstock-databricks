"""`Budget.turn_s`: min(cap, 1200 - E - pre - 75), `None` under 30 s; the rows are the proof."""

import pytest

from weather_signal.agent.budget import (
    DIRECT_CAP_S,
    DIRECT_PRE_S,
    GATEWAY_CAP_S,
    GATEWAY_PRE_S,
    HARD_S,
    RESERVE_S,
    SOFT_S,
    Budget,
)


def at(e: float) -> Budget:
    return Budget(0.0, clock=lambda: e)


GW = {"pre": GATEWAY_PRE_S, "cap": GATEWAY_CAP_S}
DIRECT = {"pre": DIRECT_PRE_S, "cap": DIRECT_CAP_S}


@pytest.mark.parametrize(
    ("e", "kw", "turn", "end"),
    [
        (1020, GW, 45, 1200),  # Connect read 120 s
        (1005, DIRECT, 90, 1200),  # pre-check failure
        (900, GW, 150, 1185),  # 503 on every prompt
        (0, GW, 150, 285),
    ],
)
def test_case_table(e, kw, turn, end):
    got = at(e).turn_s(**kw)
    assert got == turn
    assert e + kw["pre"] + got + RESERVE_S == end


def test_none_past_the_floor():
    assert at(1036).turn_s(**GW) is None
    assert at(1035).turn_s(**GW) == 30


def test_floor_boundary():
    assert at(1200 - 60 - 75 - 29.9).turn_s(**GW) is None
    assert at(1200 - 60 - 75 - 30).turn_s(**GW) == 30


def test_offset_moves_elapsed():
    assert Budget(0.0, clock=lambda: 0.0, offset_s=1100).turn_s(**DIRECT) is None


def test_sweep_never_past_soft_end():
    # Holds by construction; kept for the None boundary across both sizings.
    for e in range(0, 1261):
        for kw in (GW, DIRECT):
            t = at(e).turn_s(**kw)
            assert t is None or e + kw["pre"] + t + RESERVE_S <= SOFT_S


def test_hard_deadline():
    assert (SOFT_S, HARD_S) == (1200, 1260)


def test_floor_is_on_the_hour_not_the_cap():
    # A cap under 30 s (the process tests' seam) still sizes the turn.
    assert at(0).turn_s(pre=60, cap=2) == 2
