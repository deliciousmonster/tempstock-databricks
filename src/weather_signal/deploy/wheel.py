"""The deploy's tag: a digest of every source tree the bundle ships, which names the upload folder.

`bundle plan` never compares a wheel's bytes or a synced file's, so a code-only change left every
job pointing at the same `artifacts/.internal/weather_signal-0.1.0-py3-none-any.whl` and nothing
redeployed; serverless can also keep an environment built from a wheel of the same path. With the
tag in `workspace.artifact_path`, changed code is a changed library path, which the plan shows.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

# The wheel (pyproject.toml, src/weather_signal), the pipeline's files, the App and the dashboard.
TREES = ("pyproject.toml", "src", "app", "aibi")
APP = ("app",)  # what `bundle run ui_app` deploys
SKIP_NAMES = (".DS_Store",)
LENGTH = 12


def _skipped(rel: Path) -> bool:
    return "__pycache__" in rel.parts or rel.suffix == ".pyc" or rel.name in SKIP_NAMES


def _files(repo: Path, trees: Iterable[str]) -> list[Path]:
    out = []
    for name in trees:
        p = repo / name
        found = sorted(p.rglob("*")) if p.is_dir() else [p]
        out += [f for f in found if f.is_file() and not _skipped(f.relative_to(repo))]
    return out


def digest(repo: Path, trees: Iterable[str]) -> str:
    """The same files give the same digest; any changed, added or removed file gives another."""
    h = hashlib.sha256()
    for f in _files(repo, trees):
        h.update(f.relative_to(repo).as_posix().encode() + b"\0")
        h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()[:LENGTH]


def tag(repo: Path) -> str:
    return digest(repo, TREES)
