"""Each Claude turn sized from what is left of the hour: `min(cap, 1200 - E - pre - 75)`, or none under 30 s."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

SOFT_S = 1200.0
HARD_S = 1260.0
RESERVE_S = 75.0
FLOOR_S = 30.0
GATEWAY_PRE_S = 60.0
DIRECT_PRE_S = 30.0
GATEWAY_CAP_S = 150.0
DIRECT_CAP_S = 120.0


@dataclass
class Budget:
    """Elapsed time since the worker started; `clock` and `offset_s` are injectable for tests."""

    start: float
    clock: Callable[[], float] = time.monotonic
    offset_s: float = field(default=0.0)

    def elapsed(self) -> float:
        return self.clock() - self.start + self.offset_s

    def turn_s(self, *, pre: float, cap: float) -> float | None:
        """The bound for a turn about to connect, or `None` when under 30 s of the hour would remain."""
        left = SOFT_S - self.elapsed() - pre - RESERVE_S
        return None if left < FLOOR_S else min(cap, left)
