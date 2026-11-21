"""`weather-signal archive export`: a deployment's raw and ledger volumes, copied to a local folder
in the data folder's layout (`raw/<source>/...`, `ledger/...`, `manifest.json` with SHA-256).

It reads the workspace and writes only below `--out`, never into the data folder's `raw/`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from weather_signal.deploy import proof_archive as pa
from weather_signal.deploy import scope
from weather_signal.deploy.cli import Stop, Unknown
from weather_signal.deploy.context import Ctx

DATA = Path.home() / "Developer/weather-signal-data"  # REQ-018: the local fetcher alone writes its raw/
TARGETS = ("dev", "prod")
MANIFEST = "manifest.json"


@dataclass
class Source:
    ctx: Ctx
    read: Callable[[str], bytes | None]


def refusals(out: Path, data: Path) -> list[str]:
    """Why `out` may not take an export: its raw/ would be the data folder's, or it holds files."""
    mine, theirs = (out / "raw").resolve(), (data / "raw").resolve()
    if mine == theirs or mine.is_relative_to(theirs) or theirs.is_relative_to(mine):
        return [f"{out} would write into {data / 'raw'}, which only the local fetcher writes; "
                "pass a different --out"]  # fmt: skip
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        return [f"{out} is not an empty folder; pass a new --out"]
    return []


def manifest(s: scope.Scope, now: str, copied: dict[str, dict[str, bytes]]) -> dict[str, Any]:
    """The data folder's manifest keys for what was copied; `bootstrap --seed-from` reads `raw/`."""
    files = [
        {"path": f"{name}/{path}", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        | ({"source": path.split("/", 1)[0]} if name == "raw" else {})
        for name, got in copied.items()
        for path, data in sorted(got.items())
    ]
    return {"schema_version": 1, "generated_at": now, "catalog": s.catalog,
            "prefix": s.prefix, "files": files}  # fmt: skip


def export(src: Source, out: Path, data: Path = DATA) -> list[str]:
    """Copy every raw and ledger file to `out`, read back at its listed size, then the manifest."""
    if bad := refusals(out, data):
        raise Stop(2, "; ".join(bad))
    got = pa.volumes(src, {"raw": None, "ledger": None}, must_exist=True)
    lines = [pa.copy(out / name, name, volume, files) for name, (volume, files) in got.items()]
    doc = manifest(src.ctx.scope, src.ctx.now().isoformat(), {k: v[1] for k, v in got.items()})
    pa.put(out / MANIFEST, (json.dumps(doc, indent=1) + "\n").encode())
    return [*lines, f"{MANIFEST}: {len(doc['files'])} files with their SHA-256"]


def build_scope(a: argparse.Namespace) -> scope.Scope:
    """dev_ unless `--target prod` is given; scratch and any other prefix are refused."""
    if a.x_target not in TARGETS:
        raise scope.ScopeError(f"--target {a.x_target!r} is refused; export takes dev or prod")
    s = scope.build(a.x_profile, a.x_target, a.x_prefix, a.x_catalog)
    if a.schema_prefix not in ("", s.prefix):
        raise scope.ScopeError(f"--schema-prefix {a.schema_prefix!r} is not {s.target}'s prefix")
    return s


def real_source(s: scope.Scope) -> Source:
    from weather_signal.deploy import command
    from weather_signal.ingest.files import Files

    ns = argparse.Namespace(seed_from=None, skip_app=True, warehouse_name=None, live=False,
                            b_catalog=s.catalog)  # fmt: skip
    return Source(command.real_ctx(s, ns), Files.from_sdk(s.profile).read)


def main(a: argparse.Namespace, make: Callable[[scope.Scope], Source] | None = None) -> int:
    try:
        s = build_scope(a)
        if bad := refusals(a.out, DATA):
            raise Stop(2, "; ".join(bad))
        lines = export((make or real_source)(s), a.out, DATA)
    except scope.ScopeError as err:
        sys.stdout.write(f"archive export: {err}\n")
        return 2
    except (Stop, Unknown) as err:
        sys.stdout.write(f"archive export: stopped: {err}\n")
        return err.code if isinstance(err, Stop) else 1
    for line in lines:
        sys.stdout.write(f"archive export: {line}\n")
    return 0


def add_parser(sub: Any) -> None:
    g = sub.add_parser("archive", help="the raw archive").add_subparsers(dest="archive_cmd")
    g.required = True
    e = g.add_parser("export", help="copy a deployment's raw and ledger volumes to a local folder")
    e.add_argument("--profile", dest="x_profile", required=True)
    e.add_argument("--target", dest="x_target", default="dev", help="dev (default) or prod")
    e.add_argument("--schema-prefix", dest="x_prefix", help="dev_ for dev; prod takes none")
    e.add_argument("--catalog", dest="x_catalog", default="workspace")
    e.add_argument("--out", type=lambda v: Path(v).expanduser(), required=True,
                   help="a new folder, never the data folder itself")  # fmt: skip
    e.set_defaults(fn=main)
