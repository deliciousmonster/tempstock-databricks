"""Spans a source answered well-formed but empty: one ledger entry each, rechecked on a schedule.

An entry is `<ledger>/unavailable/<source>/<slot>.json`. Catch-up skips the span until
RECHECK_AFTER has passed since the last check, refetches it at most RECHECKS times, and after
that leaves it unavailable for good. A recheck that lands deletes the entry.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING

from weather_signal.ingest.files import Unreadable

if TYPE_CHECKING:
    from weather_signal.ingest.catchup import Volume

RECHECK_AFTER = timedelta(days=7)
RECHECKS = 3


@dataclass(frozen=True)
class Mark:
    source: str
    slot: str
    reason: str
    marked: str  # the day it was first found empty
    last_checked: str
    rechecks: int = 0

    def due(self, today: date) -> bool:
        """A recheck is owed: fewer than RECHECKS so far and RECHECK_AFTER since the last check."""
        try:
            waited = today - date.fromisoformat(self.last_checked)
        except ValueError:
            waited = RECHECK_AFTER  # a date that does not parse is due now, not a crash
        future = waited < timedelta(0)  # a skewed or hand-edited date would skip the span for good
        return self.rechecks < RECHECKS and (future or waited >= RECHECK_AFTER)

    def describe(self) -> str:
        final = "; unavailable for good" if self.rechecks >= RECHECKS else ""
        return (
            f"{self.source} {self.slot}: {self.reason} (marked {self.marked}, rechecked "
            f"{self.rechecks} of {RECHECKS}, last {self.last_checked}{final})"
        )


class Unwritten(Exception):
    """A mark whose write did not land: the span would be refetched or counted wrong."""


def _root(ledger: str, source: str) -> str:
    return f"{ledger}/unavailable/{source}"


def marks(vol: Volume, ledger: str, sources: tuple[str, ...]) -> dict[tuple[str, str], Mark]:
    """Every mark for `sources`, by (source, slot); an entry that does not parse is skipped."""
    out: dict[tuple[str, str], Mark] = {}
    for source in sources:
        root = _root(ledger, source)
        for name, is_dir in vol.list(root):
            m = None if is_dir else _parse(vol.read(f"{root}/{name}"))
            if m is not None:
                out[(m.source, m.slot)] = m
    return out


def _parse(data: bytes | None) -> Mark | None:
    """The mark `data` holds; None when it does not parse or a field has the wrong type."""
    try:
        m = Mark(**json.loads(data)) if data is not None else None
    except (TypeError, ValueError):
        return None
    if m is None or not isinstance(m.rechecks, int) or isinstance(m.rechecks, bool):
        return None
    texts = (m.source, m.slot, m.reason, m.marked, m.last_checked)
    return m if all(isinstance(x, str) for x in texts) else None


def record(
    vol: Volume, ledger: str, prior: Mark | None, found: tuple[str, str, str], today: date
) -> Mark:
    """Write the mark for `found` (source, slot, reason): a new one, or `prior` one recheck on."""
    source, slot, reason = found
    if prior is None:
        m = Mark(source, slot, reason, today.isoformat(), today.isoformat())
    else:
        m = replace(
            prior, reason=reason, last_checked=today.isoformat(), rechecks=prior.rechecks + 1
        )
    path = f"{_root(ledger, source)}/{slot}.json"
    if prior is not None and not vol.delete(path):
        raise Unwritten(path)
    got = vol.put_if_absent(path, json.dumps(asdict(m)).encode())
    if got == "exists":
        return _settle(vol, path, m)
    if got != "created":
        raise Unwritten(path)
    return m


def _settle(vol: Volume, path: str, m: Mark) -> Mark:
    """A mark was already at `path`: an overlapping run's, kept as written, or one that does not
    parse, replaced by `m`."""
    try:
        there = _parse(vol.read(path))
    except Unreadable as err:
        raise Unwritten(path) from err
    if there is not None and (there.source, there.slot) == (m.source, m.slot):
        return there
    if not vol.delete(path) or vol.put_if_absent(path, json.dumps(asdict(m)).encode()) != "created":
        raise Unwritten(path)
    return m


def clear(vol: Volume, ledger: str, m: Mark) -> None:
    """The span landed on a recheck; its mark goes. One left behind is moot: the landing covers it."""
    vol.delete(f"{_root(ledger, m.source)}/{m.slot}.json")
