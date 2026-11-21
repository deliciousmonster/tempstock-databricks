"""Phase seed: the raw-archive folder the fetch writes, verified and uploaded to raw.

The manifest lists `files[]` with `path` (`raw/<source>/...`), `bytes` and `sha256`. Price envelopes
for a ticker outside `config.TICKERS` stay in the archive and are not uploaded.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import zlib
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from weather_signal import config
from weather_signal.deploy.cli import Stop, Unknown
from weather_signal.ingest import envelope
from weather_signal.ingest.files import Store

PRICE_SOURCES = ("yahoo", "alphavantage")
ListVolume = Callable[[str], Mapping[str, int]]


@dataclass(frozen=True)
class Entry:
    path: str  # relative to the raw volume: <source>/yyyy/mm/dd/hh/<key>.json.gz
    size: int
    sha256: str


def load(root: Path) -> tuple[list[Entry], list[str]]:
    """The archive's files and notes on what it lacks. A listed file whose bytes differ is Stop(2);
    one not on disk is left out and noted; a file the manifest does not list is hashed here."""
    try:
        doc = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        files = [
            Entry(f["path"].removeprefix("raw/"), int(f["bytes"]), f["sha256"])
            for f in doc["files"]
            if str(f["path"]).startswith("raw/")  # fetch/ and state/ are the fetcher's own
        ]
    except FileNotFoundError:
        files = []
    except (OSError, ValueError, KeyError, TypeError) as err:
        raise Stop(2, f"{root}/manifest.json is not an archive manifest: {err}") from err
    out, absent, differ = [], [], []
    for e in files:
        p = root / "raw" / e.path
        if not p.is_file():
            absent.append(e.path)
        elif p.stat().st_size != e.size or _sha256(p) != e.sha256:
            differ.append(f"raw/{e.path}")
        else:
            out.append(e)
    if differ:
        raise Stop(2, f"archive {root} does not verify, bytes differ: " + "; ".join(differ[:10]))
    listed = {e.path for e in files}
    extra = 0
    for p in sorted((root / "raw").rglob("*")) if (root / "raw").is_dir() else []:
        rel = p.relative_to(root / "raw").as_posix()
        if p.is_file() and rel not in listed and not p.name.startswith("."):
            out.append(Entry(rel, p.stat().st_size, _sha256(p)))
            extra += 1
    notes = []
    if absent:
        notes.append(f"{len(absent)} manifest files not on disk, left out (first: raw/{absent[0]})")
    if extra:
        notes.append(f"{extra} files not in the manifest, hashed here")
    if not out:
        notes.append("no raw files: nothing to seed yet")
    return out, notes


def coverage(root: Path, files: Iterable[Entry]) -> list[str]:
    """Per source: files, and the slots they cover as spans of days; a gap of a day starts a span."""
    slots: dict[str, list[tuple[date, date]]] = {}
    count: Counter[str] = Counter()
    for e in files:
        source = e.path.split("/", 1)[0]
        count[source] += 1
        try:
            env = json.loads(gzip.decompress((root / "raw" / e.path).read_bytes()))
            lo, hi = str(env["slot"]).split("..")
            slots.setdefault(source, []).append((date.fromisoformat(lo), date.fromisoformat(hi)))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    out = []
    for source in sorted(count):
        spans: list[list[date]] = []
        for a, b in sorted(slots.get(source, [])):
            if spans and a <= spans[-1][1] + timedelta(days=1):
                spans[-1][1] = max(spans[-1][1], b)
            else:
                spans.append([a, b])
        shown = ", ".join(f"{a}..{b}" for a, b in spans[:4]) + (" ..." if len(spans) > 4 else "")
        out.append(f"{source}: {count[source]} files" + (f", {shown}" if spans else ""))
    return out


def absent_tickers(root: Path, files: Iterable[Entry], tickers: tuple[str, ...]) -> list[str]:
    """The configured tickers no kept price envelope names."""
    have = set()
    for e in files:
        if e.path.split("/", 1)[0] in PRICE_SOURCES:
            have.add(ticker(json.loads(gzip.decompress((root / "raw" / e.path).read_bytes()))))
    return [t for t in tickers if t not in have]


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def ticker(env: Mapping[str, Any]) -> str:
    """The ticker a price envelope asked for, from its recorded request; Unknown when absent."""
    req = env.get("request") or {}
    if env.get("source") == "alphavantage":
        sym = (req.get("params") or {}).get("symbol")
    else:
        sym = str(req.get("endpoint") or "").rstrip("/").rsplit("/", 1)[-1]
    if not sym:
        raise Unknown(f"cannot read the ticker of a {env.get('source')} envelope")
    return str(sym)


def kept(
    root: Path, files: Iterable[Entry], tickers: tuple[str, ...] = config.TICKERS
) -> tuple[list[Entry], Counter[str]]:
    """The entries to seed, and per ticker the price envelopes left out for naming another stock."""
    out: list[Entry] = []
    skipped: Counter[str] = Counter()
    for e in files:
        if e.path.split("/", 1)[0] in PRICE_SOURCES:
            try:
                env = json.loads(gzip.decompress((root / "raw" / e.path).read_bytes()))
            except (OSError, ValueError) as err:
                raise Unknown(f"cannot read raw/{e.path}") from err
            t = ticker(env)
            if t not in tickers:
                skipped[t] += 1
                continue
        out.append(e)
    return out, skipped


def skipped_line(skipped: Counter[str]) -> str:
    if not skipped:
        return "no price envelope skipped"
    names = ", ".join(sorted(skipped))
    return f"{sum(skipped.values())} price envelopes for tickers outside config skipped: {names}"


def _request_key(path: str, data: bytes | None) -> str | None:
    """The landing key an envelope's own request recomputes to, when it is whole and from `path`'s
    source; None for anything else."""
    try:
        env = envelope.decode(data or b"")
        req = env["request"]
        key = envelope.landing_key(env["source"], req["endpoint"], req["params"], env["slot"])
        body_ok = hashlib.sha256(env["body_text"].encode()).hexdigest() == env["body_sha256"]
    except (OSError, EOFError, zlib.error, ValueError, KeyError, TypeError, AttributeError):
        return None
    return key if body_ok and env["source"] == path.split("/", 1)[0] else None


def same_request(path: str, archived: bytes, landed: bytes | None) -> bool:
    """Both copies are whole envelopes of the request the name's key encodes: one request fetched
    at two moments, whose bodies may differ by a byte."""
    key = path.rsplit("/", 1)[-1].removesuffix(".json.gz")
    return _request_key(path, archived) == key == _request_key(path, landed)


def missing(
    want: Iterable[Entry],
    have: Mapping[str, int],
    volume: str,
    *,
    same: Callable[[Entry], bool] = lambda e: False,
) -> list[Entry]:
    """Entries absent from the volume. One present at another size is kept when `same` says it is
    the same request, else Stop(1); nothing is overwritten either way."""
    out = []
    for e in want:
        size = have.get(e.path)
        if size is None:
            out.append(e)
        elif size != e.size and not same(e):
            raise Stop(
                1,
                f"{volume}/{e.path} holds {size} bytes, the archive {e.size}, and is not a copy of "
                "the same request; nothing overwritten. "
                f"Remove it with `databricks fs rm dbfs:{volume}/{e.path}`, then rerun.",
            )
    return out


def upload(root: Path, todo: Iterable[Entry], store: Store, volume: str) -> int:
    n = 0
    for e in todo:
        out = store.put_if_absent(f"{volume}/{e.path}", (root / "raw" / e.path).read_bytes())
        if out == "unknown":
            raise Unknown(f"upload of {e.path} did not answer")
        n += out == "created"
    return n


def files_under(root: Path) -> list[str]:
    """Every file below `root`, as posix paths relative to it; none when it is absent."""
    if not root.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def restore(root: Path, todo: Iterable[str], store: Store, volume: str) -> int:
    """Create each file in the volume if absent: a ledger claim is kept, never overwritten."""
    n = 0
    for rel in todo:
        out = store.put_if_absent(f"{volume}/{rel}", (root / rel).read_bytes())
        if out == "unknown":
            raise Unknown(f"restore of {rel} did not answer")
        n += out == "created"
    return n


def list_volume_sdk(profile: str) -> ListVolume:
    """Sizes of every file under a volume folder, keyed by the path below it (Files API listing)."""

    def run(root: str) -> dict[str, int]:
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.errors import NotFound

        w, out, todo = WorkspaceClient(profile=profile), {}, [root]
        while todo:
            d = todo.pop()
            try:
                for f in w.files.list_directory_contents(d):
                    if f.is_directory:
                        todo.append(str(f.path).rstrip("/"))
                    else:
                        out[str(f.path).removeprefix(root + "/")] = int(f.file_size or 0)
            except NotFound:
                if d != root:
                    raise Unknown(f"{d} vanished while it was listed") from None
            except Exception as err:
                raise Unknown(f"listing {d}: {type(err).__name__}") from err
        return out

    return run
