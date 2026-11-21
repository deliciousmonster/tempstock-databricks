"""Which checkout bootstrap runs from: the LaunchAgent it installs keeps that checkout's venv."""

from __future__ import annotations

from pathlib import Path


def linked_main(repo: Path) -> Path | None:
    """The main checkout when `repo` is a linked worktree (`.git` is a file); None otherwise."""
    dot = repo / ".git"
    if not dot.is_file():
        return None
    gitdir = Path(dot.read_text(encoding="utf-8").strip().removeprefix("gitdir:").strip())
    gitdir = gitdir if gitdir.is_absolute() else (repo / gitdir).resolve()
    return gitdir.parents[2] if gitdir.parent.name == "worktrees" else gitdir.parent
