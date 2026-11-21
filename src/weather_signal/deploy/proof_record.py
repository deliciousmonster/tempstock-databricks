"""The run record `run.json`: what a run archived, written before it destroys anything, so a
`--resume` binds to that run and to nothing else.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from weather_signal.deploy.proof import Proof
    from weather_signal.deploy.proof_archive import Archived
    from weather_signal.deploy.scope import Scope

MANIFEST = "run.json"


def write(p: Proof, got: Archived) -> None:
    """Bind the folder to this run: its scope, its data folder, and every ledger file archived."""
    s = p.ctx.scope
    doc = {
        "run_id": uuid.uuid4().hex,
        "profile": s.profile,
        "prefix": s.prefix,
        "seed_from": str(p.seed_from),
        "archived_at": p.ctx.now().isoformat(),
        "ledger": {k: len(v) for k, v in sorted(got.files.get("ledger", {}).items())},
        "destroyed_at": None,
    }
    p.write(MANIFEST, json.dumps(doc, indent=1) + "\n")


def _load(p: Proof) -> dict[str, Any] | None:
    try:
        doc = json.loads((p.out / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def destroyed(p: Proof) -> None:
    """Record that this run's destroy finished with no dev residue."""
    doc = _load(p) or {}
    doc["destroyed_at"] = p.ctx.now().isoformat()
    p.write(MANIFEST, json.dumps(doc, indent=1) + "\n")


def was_destroyed(p: Proof) -> bool:
    return bool((_load(p) or {}).get("destroyed_at"))


def _verified(p: Proof) -> bool:
    try:
        doc = json.loads((p.out / "verify.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and doc.get("verdict") == "PASS"


def resume_refusals(p: Proof) -> list[str]:
    """Why this folder cannot be resumed: not a run's record, another scope or data folder, a
    ledger file it lists that is not on disk, or a run that already passed verify."""
    doc = _load(p)
    if doc is None:
        return [f"{p.out} holds no {MANIFEST}: no run archived there, nothing to resume"]
    s, out = p.ctx.scope, []
    want = {"profile": s.profile, "prefix": s.prefix, "seed_from": str(p.seed_from)}
    out += [
        f"{MANIFEST} names {k} {doc.get(k)!r}, not {v!r}"
        for k, v in want.items()
        if doc.get(k) != v
    ]
    ledger = p.out / "archive" / "ledger"
    for path, size in (doc.get("ledger") or {}).items():
        f = ledger / path
        if not f.is_file() or f.stat().st_size != size:
            out.append(f"archive/ledger/{path} is missing or not {size} bytes")
    if doc.get("destroy_failed_at"):
        out.append(f"the run in {p.out} closed when its destroy failed with nothing deleted; "
                   "run the proof again with a new --out")  # fmt: skip
    if _verified(p):
        out.append(f"the run in {p.out} completed (verify passed); nothing to resume")
    return out


def _update(p: Proof, **fields: Any) -> None:
    doc = _load(p) or {}
    doc.update(fields)
    p.write(MANIFEST, json.dumps(doc, indent=1) + "\n")


def pointer(p: Proof) -> Path:
    """Beside bootstrap's lock: the --out of a run whose destroy began and whose bootstrap has not
    yet exited 0, so a fresh run with another --out cannot become the recovery."""
    return _pointer_at(p.ctx.state_dir, p.ctx.scope)


def _pointer_at(state_dir: Path, s: Scope) -> Path:
    return state_dir / f"proof-{s.target}-{s.prefix}.json"


def release(state_dir: Path, s: Scope, ledger_from: Path, seed_from: Path | None) -> bool:
    """A bootstrap that exited 0 restored the pointed run's ledger, as that run's own would: free
    the next fresh run. Kept unless `--ledger-from` holds every file the run archived, at its size
    (a run.json recording none has none to restore), and `--seed-from` is the run's data folder."""
    path = _pointer_at(state_dir, s)
    try:
        out = Path(json.loads(path.read_text(encoding="utf-8"))["out"])
        doc = json.loads((out / MANIFEST).read_text(encoding="utf-8"))
        files, seed = dict(doc["ledger"]), Path(doc["seed_from"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if ledger_from.expanduser().resolve() != (out / "archive" / "ledger").resolve():
        return False
    if seed_from is None or seed_from.expanduser().resolve() != seed.resolve():
        return False
    if any(_size(ledger_from.expanduser() / f) != n for f, n in files.items()):
        return False
    path.unlink(missing_ok=True)
    return True


def _size(f: Path) -> int | None:
    return f.stat().st_size if f.is_file() else None


def destroy_began(p: Proof, keys: list[str]) -> None:
    """Record the resources the destroy was planned for (the first time only) and point at this run."""
    if not (_load(p) or {}).get("destroy_keys"):
        _update(p, destroy_began_at=p.ctx.now().isoformat(), destroy_keys=sorted(keys))
    path = pointer(p)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"out": str(p.out)}) + "\n", encoding="utf-8")


def destroy_failed(p: Proof) -> None:
    """A destroy that deleted nothing: close the run and free the next fresh one."""
    _update(p, destroy_failed_at=p.ctx.now().isoformat())
    if pending(p) == p.out:
        pointer(p).unlink(missing_ok=True)


def bootstrap_began(p: Proof) -> None:
    _update(p, bootstrap_began_at=p.ctx.now().isoformat())


def bootstrap_ended(p: Proof, rc: int) -> None:
    """A bootstrap that exits 0 has restored the ledger: this run no longer blocks a fresh one."""
    _update(p, bootstrap_rc=rc)
    if rc == 0 and pending(p) == p.out:
        pointer(p).unlink(missing_ok=True)


def pending(p: Proof) -> Path | None:
    try:
        out = Path(json.loads(pointer(p).read_text(encoding="utf-8"))["out"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return out if (out / MANIFEST).is_file() else None


def bootstrap_began_in(out: Path) -> bool:
    try:
        return bool(
            json.loads((out / MANIFEST).read_text(encoding="utf-8")).get("bootstrap_began_at")
        )
    except (OSError, ValueError, AttributeError):
        return False


def may_finish_destroy(p: Proof, keys: list[str]) -> list[str]:
    """[] when a resume may run `bundle destroy` again: this run's destroy began, its bootstrap
    did not, and every resource still deployed is one the destroy was planned for."""
    doc = _load(p) or {}
    if not doc.get("destroy_keys"):
        return ["this run began no destroy"]
    if doc.get("destroyed_at"):
        return ["this run's destroy finished, so dev was deployed again since"]
    if doc.get("bootstrap_began_at"):
        return ["this run's bootstrap has started, so dev was deployed again"]
    extra = sorted(set(keys) - set(doc["destroy_keys"]))
    return [f"{k}: deployed, not in the destroy this run began" for k in extra]
