"""U2: a call's integrity state, computed when it is shown. Mirrors `app/static/integrity.js`.

The only Python copy: the App ships it; the tests load it by path. Dependency-free, Python 3.11. Both implementations run every case in `tests/fixtures/integrity_cases.json`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

ISO = re.compile(
    r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])T([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d+)?"
    r"(Z|[+-]\d{2}:[0-5]\d)$"
)
MAX_AGE = timedelta(minutes=75)
SKEW = timedelta(minutes=5)
MAX_OFFSET = timedelta(hours=18)

LABELS = {
    "not_set_up": "unknown: guard not set up",
    "held": "held",
    "flagged": "flagged",
    "unreadable": "unknown: integrity unreadable",
    "stale": "unknown: integrity as of {read_at}",
    "awaiting": "unknown: awaiting guard check",
    "verified": "verified",
}


class Unreadable(ValueError):
    """A value the rule cannot read; the state is `unreadable`, never a guess."""


def ts(v: object, *, required: bool = True) -> datetime | None:
    """ISO 8601 with `Z` or an offset of at most 18 hours, a real date, year 1 or later."""
    if v is None and not required:
        return None
    if not isinstance(v, str) or not ISO.match(v):
        raise Unreadable(repr(v))
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as err:  # well-formed but impossible: Feb 30, year 0000
        raise Unreadable(v) from err
    off = t.utcoffset()
    if off is None or abs(off) > MAX_OFFSET:
        raise Unreadable(v)
    return t


def problems(v: object) -> bool:
    """True when `audit_problem` names a problem; null, "" and [] name none."""
    if v is None or v == "" or v == []:
        return False
    if isinstance(v, str) or (isinstance(v, list) and all(isinstance(x, str) for x in v)):
        return True
    raise Unreadable(repr(v))


def state(row: Mapping[str, Any], now: object) -> str:
    """The U2 rule, in order; returns a key of `LABELS`."""
    if row.get("guarded_tables") != 2 or row.get("open_incident") is None:
        return "not_set_up"
    if row["open_incident"]:
        return "held"
    try:
        if problems(row.get("audit_problem")):
            return "flagged"
        n, read_at, as_of = ts(now), ts(row.get("read_at")), ts(row.get("as_of"))
        written = ts(row.get("written_at"), required=False)
        post = ts(row.get("post_verified_as_of"), required=False)
    except Unreadable:
        return "unreadable"
    if n is None or read_at is None or as_of is None:
        return "unreadable"
    age = n - read_at
    if age > MAX_AGE or age < -SKEW:
        return "stale"
    if post is None or post <= (written or as_of):
        return "awaiting"
    return "verified"


def label(key: str, read_at: object = None) -> str:
    return LABELS[key].format(read_at=read_at)
