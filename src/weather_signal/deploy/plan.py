"""`bundle plan -o json` read as the bundle phase's done, with the default-deny guard."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from weather_signal.deploy.cli import Stop, Unknown

NO_CHANGE = "skip"
CHANGES = frozenset({"create", "update", "update_id", "resize", "delete", "recreate"})
DESTRUCTIVE = frozenset({"delete", "recreate"})
# A job keeps no data and the bundle recreates it whole; every other type is denied.
SAFE_TYPES = frozenset({"jobs"})


@dataclass(frozen=True)
class Planned:
    key: str
    action: str

    @property
    def type(self) -> str:
        parts = self.key.split(".")
        if len(parts) < 3 or parts[0] != "resources":
            raise Unknown(f"plan key {self.key!r} is not resources.<type>.<key>")
        return parts[1]


def entries(doc: Any) -> list[Planned]:
    if not isinstance(doc, Mapping) or not isinstance(doc.get("plan"), Mapping):
        raise Unknown("bundle plan output has no `plan` object")
    out = []
    for key, v in doc["plan"].items():
        action = v.get("action") if isinstance(v, Mapping) else None
        if action != NO_CHANGE and action not in CHANGES:
            raise Unknown(f"bundle plan: {key} has unrecognized action {action!r}")
        out.append(Planned(str(key), str(action)))
    return out


def changes(doc: Any) -> list[Planned]:
    """The entries a deploy would change; raises Stop(1) on a destructive entry the guard denies."""
    planned = entries(doc)
    denied = [p for p in planned if p.action in DESTRUCTIVE and p.type not in SAFE_TYPES]
    if denied:
        lines = "\n".join(f"  {p.action} {p.key}" for p in denied)
        raise Stop(1, f"bundle plan would destroy data or identity; nothing deployed:\n{lines}")
    return [p for p in planned if p.action != NO_CHANGE]
