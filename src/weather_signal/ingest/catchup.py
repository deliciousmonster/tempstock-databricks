"""The ingest job's catch-up: each source from its configured start to now, inside a budget.

The raw volume may hold a seeded archive that ends days or months back, or has holes inside it.
A catch-up run reads the newest envelope per source (per ticker for Yahoo) and fetches the gap
after it, then the holes between the source's start and that end: ERA5 and Previous Runs in
backfill's chunks under a weighted budget, the tail first, Yahoo as one full history per ticker
whose landed histories do not span its start to the last close. A weather source with nothing
landed is left to the data folder's fetch. Alpha Vantage keeps its own ledger.
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from weather_signal import config
from weather_signal.ingest import backfill, hourly, unavailable, yahoo_history
from weather_signal.ingest.fetch import Transport
from weather_signal.ingest.files import Outcome, Presence, Unreadable
from weather_signal.ingest.om_budget import Ledger
from weather_signal.sessions import Calendar

log = logging.getLogger(__name__)
# Weighted Open-Meteo units per catch-up run; one ERA5 year is ~391, the free day is 7000.
BUDGET = 2000.0
ERA5_LAG = timedelta(days=8)
PREVIOUS_RUNS_LAG = timedelta(days=2)
# Each weather source's first day and chunk shape, as the data folder's backfill plans them.
STARTS = {"open-meteo-era5": date(1961, 1, 1), "open-meteo-previous-runs": date(2021, 3, 24)}
SHAPES = {"open-meteo-era5": (365, 3), "open-meteo-previous-runs": (14, 8)}
# More sessions than this with the newest close unsettled since the last landing: broken.
SETTLE_SESSIONS = 3


class Volume(Protocol):
    def put_if_absent(self, path: str, data: bytes) -> Outcome: ...
    def exists(self, path: str) -> Presence: ...
    def read(self, path: str) -> bytes | None: ...
    def list(self, path: str) -> list[tuple[str, bool]]: ...
    def delete(self, path: str) -> bool: ...


def slot_end(slot: str) -> date:
    """The last day a slot holds: `a..b` is b; a steady slot (a day or an hour) holds the day before."""
    if ".." in slot:
        return date.fromisoformat(slot.split("..", 1)[1])
    return date.fromisoformat(slot[:10]) - timedelta(days=1)


def slot_start(slot: str, source: str) -> date:
    """The first day a slot holds: `a..b` is a; a steady ERA5 day holds ERA5_DAYS, an hour one."""
    if ".." in slot:
        return date.fromisoformat(slot.split("..", 1)[0])
    width = hourly.ERA5_DAYS if source == "open-meteo-era5" else 1
    return slot_end(slot) - timedelta(days=width - 1)


def spans(
    vol: Volume, root: str, source: str, ticker: str | None = None
) -> list[tuple[date, date]]:
    """The days each matching envelope holds. Only hour-00 folders are read; a later hour holds
    one steady Previous Runs hour, whose day is the one before its folder's."""
    out = []
    for leaf in _leaves(vol, f"{root}/{source}", 4):
        y, m, d, h = leaf.rsplit("/", 4)[1:]
        if h != "00":
            day = date(int(y), int(m), int(d)) - timedelta(days=1)
            out.append((day, day))
            continue
        for slot in _slots(vol, leaf, ticker):
            out.append((slot_start(slot, source), slot_end(slot)))
    return out


def missing(held: list[tuple[date, date]], start: date, through: date) -> list[tuple[date, date]]:
    """The runs of days from `start` to `through` that no span in `held` covers."""
    out, d = [], start
    for a, b in sorted(held):
        if a > d and d <= through:
            out.append((d, min(a - timedelta(days=1), through)))
        d = max(d, b + timedelta(days=1))
    if d <= through:
        out.append((d, through))
    return out


def holes(vol: Volume, root: str, ends: dict[str, date | None]) -> list[backfill.Chunk]:
    """Chunks for every hole between each weather source's start and its newest end, oldest first."""
    work: list[backfill.Chunk] = []
    for source, end in ends.items():
        if end is None:
            continue
        span_days, variables = SHAPES[source]
        for a, b in missing(spans(vol, root, source), STARTS[source], end):
            log.info("catch-up hole %s %s..%s", source, a, b)
            work += backfill.chunks(source, a, b, span_days, variables)
    return sorted(work, key=lambda c: c.start)


def _leaves(vol: Volume, path: str, depth: int) -> Iterator[str]:
    """Dated folders `yyyy/mm/dd/hh` under `path`, newest first."""
    if depth == 0:
        yield path
        return
    for name, is_dir in sorted(vol.list(path), reverse=True):
        if is_dir:
            yield from _leaves(vol, f"{path}/{name}", depth - 1)


def newest(vol: Volume, root: str, source: str, ticker: str | None = None) -> date | None:
    """The latest slot end in the newest folder holding a matching envelope; None when none does."""
    for leaf in _leaves(vol, f"{root}/{source}", 4):
        ends = [slot_end(slot) for slot in _slots(vol, leaf, ticker)]
        if ends:
            return max(ends)
    return None


def _slots(vol: Volume, leaf: str, ticker: str | None) -> Iterator[str]:
    """The slot of each envelope in `leaf`, for `ticker` alone when one is named."""
    for name, is_dir in vol.list(leaf):
        data = None if is_dir else vol.read(f"{leaf}/{name}")
        if data is None:
            continue
        env: dict[str, Any] = json.loads(gzip.decompress(data))
        url = str((env.get("request") or {}).get("endpoint") or "")
        if ticker is None or url.rstrip("/").rsplit("/", 1)[-1] == ticker:
            yield str(env["slot"])


def weather_plan(
    ends: dict[str, date | None],
    today: date,
    filling: list[backfill.Chunk] | None = None,
    skip: frozenset[tuple[str, str]] = frozenset(),
) -> list[backfill.Chunk]:
    """The chunks after each source's newest end, oldest first, then `filling`, cut to `BUDGET`;
    a chunk whose (source, slot) is in `skip` is left out."""
    out, spent = [], 0.0
    for c in _open(weather_gap(ends, today) + (filling or []), skip):
        if spent + c.cost > BUDGET:
            break
        out.append(c)
        spent += c.cost
    return out


def _open(work: list[backfill.Chunk], skip: frozenset[tuple[str, str]]) -> list[backfill.Chunk]:
    return [c for c in work if (c.source, c.slot) not in skip]


def weather_gap(ends: dict[str, date | None], today: date) -> list[backfill.Chunk]:
    """Every chunk after each source's newest end, oldest first; a source with nothing landed has none."""
    work: list[backfill.Chunk] = []
    era5, prev = ends.get("open-meteo-era5"), ends.get("open-meteo-previous-runs")
    if era5 is not None:
        work += backfill.chunks(
            "open-meteo-era5", era5 + timedelta(days=1), today - ERA5_LAG, 365, 3
        )
    if prev is not None:
        start = prev + timedelta(days=1)
        work += backfill.chunks("open-meteo-previous-runs", start, today - PREVIOUS_RUNS_LAG, 14, 8)
    return sorted(work, key=lambda c: c.start)


@dataclass
class Report:
    ends: dict[str, str] = field(default_factory=dict)
    holes: list[str] = field(default_factory=list)  # chunks inside a history, planned or deferred
    planned: int = 0
    landed: int = 0
    failed: int = 0
    deferred: int = 0  # chunks past the budget; the next 03:05 run starts from them
    left: list[str] = field(default_factory=list)
    settling: list[str] = field(default_factory=list)  # newest close not settled: no failure
    unavailable: list[str] = field(default_factory=list)  # well-formed, empty at source: no failure


def run(
    vol: Volume,
    n: config.Names,
    transport: Transport,
    cal: Calendar,
    now: datetime,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    tickers: tuple[str, ...] = config.TICKERS,
) -> Report:
    rep = Report()
    ends = {s: newest(vol, n.raw, s) for s in backfill.SOURCES}
    rep.ends = {s: str(e) for s, e in ends.items()}
    filling = holes(vol, n.raw, ends)
    rep.holes = [f"{c.source} {c.slot}" for c in filling]
    marked, skip = _marked(vol, n, now.date(), rep)
    work = weather_plan(ends, now.date(), filling, skip)
    rep.planned = len(work)
    rep.deferred = len(_open(weather_gap(ends, now.date()) + filling, skip)) - len(work)
    driver = backfill.Driver(Ledger(), clock, sleep)

    def one(c: backfill.Chunk) -> bool:
        r, prior = backfill.Report(), marked.get((c.source, c.slot))
        got = backfill.fetch_checked(c, transport, r)
        if got is None and r.unavailable:
            _mark(vol, n, (c.source, c.slot, r.unavailable), prior, now.date(), rep)
            return False
        if got is None:
            rep.failed += 1
            return False
        endpoint, params = backfill.request(c)
        slot_time = datetime.combine(c.start, datetime.min.time(), tzinfo=UTC)
        put = hourly.land(vol, n.raw, c.source, endpoint, params, c.slot, slot_time, *got)
        return _landed(vol, n, c, put, prior, rep)

    driver.run(work, lambda c: vol.exists(backfill.landing(n.raw, c)) == "present", one)
    _prices(vol, n, transport, cal, now, tickers, rep)
    return rep


def _landed(
    vol: Volume, n: config.Names, c: backfill.Chunk, put: Outcome,
    prior: unavailable.Mark | None, rep: Report,
) -> bool:  # fmt: skip
    """Count one chunk's landing; one that did not answer is a failure, the span may be missing."""
    log.info("catch-up %s %s %s", c.source, c.slot, put)
    if put == "unknown" and vol.exists(backfill.landing(n.raw, c)) == "present":
        put = "created"  # the put landed and lost its reply
    if put == "unknown":
        rep.failed += 1
        rep.left.append(f"{c.source} {c.slot}: landing did not answer")
    rep.landed += put == "created"
    if put == "created" and prior is not None:
        unavailable.clear(vol, n.ledger, prior)
    return put == "created"


def _marked(
    vol: Volume, n: config.Names, today: date, rep: Report
) -> tuple[dict[tuple[str, str], unavailable.Mark], frozenset[tuple[str, str]]]:
    """The ledger's unavailable marks, and the spans among them not yet due a recheck."""
    marked = unavailable.marks(vol, n.ledger, backfill.SOURCES)
    skip = frozenset(k for k, m in marked.items() if not m.due(today))
    rep.unavailable = [marked[k].describe() for k in sorted(skip)]
    return marked, skip


def _mark(
    vol: Volume, n: config.Names, found: tuple[str, str, str], prior: unavailable.Mark | None,
    today: date, rep: Report,
) -> None:  # fmt: skip
    """Record a span empty at source in the ledger; a mark that cannot be written is a failure."""
    try:
        rep.unavailable.append(unavailable.record(vol, n.ledger, prior, found, today).describe())
    except unavailable.Unwritten as err:
        rep.failed += 1
        rep.left.append(
            f"{found[0]} {found[1]}: {found[2]}; its unavailable mark {err} not written"
        )


def _prices(
    vol: Volume, n: config.Names, transport: Transport, cal: Calendar, now: datetime,
    tickers: tuple[str, ...], rep: Report,
) -> None:  # fmt: skip
    """A full history for each ticker with no landed one from its start to the last closed
    session. A volume that cannot be read fails that ticker alone."""
    due = cal.latest_closed_before(now)
    attempt = yahoo_history.attempt_for(vol, n, transport, cal, now)
    for t in tickers:
        try:
            held = spans(vol, n.raw, "yahoo", t)
            end = max((b for _, b in held), default=None)
            rep.ends[f"yahoo {t}"] = str(end)
            if any(a <= yahoo_history.start_for(t) and b >= due for a, b in held):
                continue
            if end is not None and end >= due:
                rep.holes.append(f"yahoo {t} from {yahoo_history.start_for(t)}")
            rep.planned += 1
            got, why = attempt(t)
            if got == "valid":
                rep.landed += 1
            elif got == "settling" and unsettled_sessions(vol, n, t, end, due) <= SETTLE_SESSIONS:
                rep.settling.append(f"yahoo {t}: {why}")
            else:
                rep.failed += 1
                rep.left.append(f"yahoo {t}: {why}" + (f"; landed through {end}" if end else ""))
        except Unreadable as err:
            rep.failed += 1
            rep.left.append(f"yahoo {t}: {err} not readable")
        except Unwritten as err:
            rep.failed += 1
            rep.left.append(f"yahoo {t}: {err} not written")


class Unwritten(Exception):
    """A settling mark whose write neither landed nor found it there: the count cannot be trusted."""


def unsettled_sessions(
    vol: Volume, n: config.Names, ticker: str, end: date | None, due: date
) -> int:
    """Mark `due` as seen unsettled, then count the marked sessions after the last landing `end`.
    A stale seed counts one; a feed that never settles counts one more each session. Marks at or
    before `end` are deleted: a landing since then means they never count again."""
    root = f"{n.ledger}/settling/yahoo/{ticker}"
    if vol.put_if_absent(f"{root}/{due}", f"{due}\n".encode()) == "unknown":
        raise Unwritten(f"{root}/{due}")
    marks = _marks(vol, root)
    for name, d in marks.items():
        if end is not None and d <= end:
            vol.delete(f"{root}/{name}")  # one left behind is pruned at the next count
    return sum(end is None or d > end for d in marks.values())


def _marks(vol: Volume, root: str) -> dict[str, date]:
    """The sessions marked under `root`, by file name; a name that is not a date is not a mark."""
    out: dict[str, date] = {}
    for name, is_dir in vol.list(root):
        try:
            out |= {} if is_dir else {name: date.fromisoformat(name)}
        except ValueError:
            continue
    return out
