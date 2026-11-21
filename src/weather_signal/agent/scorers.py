"""The evaluation's four code scorers and the per-reason thresholds the prompt states; pure functions."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

# While P >= P_MIN_H a healthy fitted-lead row stays under 29.5 h old; below it this bound
# flags healthy hours, so a test pins config.P_PROVISIONAL_H.
STALE = timedelta(hours=30)
P_MIN_H = 19
ANOMALY_MAX = 12.0
INDEX_RANGE = (-25.0, 38.0)
RET_5D_MAX = 0.25
ACTIONS = ("buy", "sell", "hold")
_ACTION_WORD = re.compile(r"\b(buy|sell)(?:s|ing)?\b", re.I)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _ts(v: Any) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def faults(row: dict[str, Any], as_of: datetime) -> tuple[str, ...]:
    """Every reason the prompt's thresholds give for `row`, in the prompt's order; none for a hold."""
    if row.get("signal_action") == "hold":
        return ()
    fk_raw, imk_raw = row.get("forecast_known_at"), row.get("inputs_max_known_at")
    fk = None if fk_raw is None else _ts(fk_raw)
    idx, anom, ret = row.get("index_c"), row.get("anomaly_c"), row.get("ret_5d")
    out = []
    if fk is not None and fk < as_of - STALE:
        out.append("stale_inputs")
    if (anom is not None and abs(anom) > ANOMALY_MAX) or (
        idx is not None and not INDEX_RANGE[0] <= idx <= INDEX_RANGE[1]
    ):
        out.append("out_of_range")
    late = fk is not None and imk_raw is not None and fk > _ts(imk_raw)
    if late or (ret is not None and abs(ret) > RET_5D_MAX):
        out.append("inconsistent_inputs")
    return tuple(out)


def downgrade_matches_fault(written: dict[str, Any], found: str | Iterable[str]) -> bool:
    """Held for one of the reasons the row breaches; the tool accepts any of them."""
    allowed = {found} if isinstance(found, str) else set(found)
    return written.get("action") == "hold" and written.get("downgrade_reason") in allowed


def no_downgrade_clean(written: dict[str, Any]) -> bool:
    return written.get("downgrade_reason") is None and written.get("action") == written.get(
        "signal_action"
    )


def _forms(v: float) -> set[str]:
    out = {f"{v:.{d}f}" for d in range(4)}
    if abs(v) < 1:
        out |= {f"{v * 100:.{d}f}" for d in range(3)}  # a return cited as a percentage
    return {s.rstrip("0").rstrip(".") if "." in s else s for s in out}


def cites_numbers(rationale: str, tool_numbers: Iterable[float]) -> bool:
    """The rationale holds at least one number the tools returned for its date."""
    said = {s.rstrip("0").rstrip(".") if "." in s else s for s in _NUMBER.findall(rationale)}
    said |= {s.lstrip("-") for s in said}
    return any(_forms(float(v)) & said for v in tool_numbers if v is not None)


def no_contradiction(rationale: str, action: str) -> bool:
    """No action word other than the row's written action, anywhere in the rationale."""
    named = {m.group(1).lower() for m in _ACTION_WORD.finditer(rationale)}
    return named <= ({action} & {"buy", "sell"})
