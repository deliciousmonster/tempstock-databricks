"""`weather-signal ui-export`: the pages and one snapshot in a folder that opens from `file://`."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import stat
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from weather_signal.serving import app_bridge, snapshot

BOUND_S = 60
# The pages live in the checkout's app/, not in the wheel; the export runs from the checkout.
APP_STATIC = Path(__file__).resolve().parents[3] / "app" / "static"


MARKER = ".weather-signal-export"


class ExportError(RuntimeError):
    """The snapshot could not be read in time, failed its check, or `out` is not the export's; nothing was written."""


def download(path: str, profile: str | None, bound_s: float = BOUND_S) -> dict[str, Any]:
    from databricks.sdk import WorkspaceClient

    def read() -> dict[str, Any]:
        resp = WorkspaceClient(profile=profile).files.download(path)
        if resp.contents is None:
            raise ExportError(f"{path}: empty response")
        return dict(json.loads(resp.contents.read()))

    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["doc"] = read()
        except BaseException as err:  # noqa: BLE001 - handed to the caller below
            box["err"] = err

    # A daemon thread, so a read still hung at the bound never holds the process open.
    t = threading.Thread(target=run, name="ui-export-download", daemon=True)
    t.start()
    t.join(bound_s)
    if t.is_alive():
        raise ExportError(f"{path}: no answer in {bound_s:.0f} s")
    if "err" in box:
        raise ExportError(f"{path}: {type(box['err']).__name__}: {box['err']}") from box["err"]
    return dict(box["doc"])


def _files(static: Path) -> dict[str, Path]:
    """Every file the export writes, by its path under `out`, and where it comes from."""
    out = {"index.html": static / "index.html"}
    for f in sorted(static.rglob("*")):
        if f.is_file() and f.name != "index.html" and "__pycache__" not in f.parts:
            out[f"static/{f.relative_to(static).as_posix()}"] = f
    return out


def _plain(rel: str) -> bool:
    """A relative path of plain names: no root, no `.` or `..`, no empty part, no NUL."""
    parts = rel.split("/")
    return (
        "\x00" not in rel
        and all(p not in ("", ".", "..") for p in parts)
        and not rel.startswith("/")
    )


def _check_marker(out: Path, wrote: set[str], force: bool) -> None:
    """The marker may list the export's own files; with --force, other plain paths are left alone."""
    try:
        listed = json.loads((out / MARKER).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as err:
        raise ExportError(f"{out / MARKER} is unreadable: {err}") from err
    if not isinstance(listed, list) or not all(isinstance(x, str) for x in listed):
        raise ExportError(f"{out / MARKER} is not a list of paths")
    odd = sorted(set(listed) - wrote)
    if bad := [x for x in odd if not _plain(x)]:
        raise ExportError(f"{out / MARKER} lists paths outside the folder: {bad[:3]}")
    if odd and not force:
        raise ExportError(
            f"{out / MARKER} lists files this export no longer writes: {odd[:3]}; pass --force to "
            "export anyway (they are left in place, nothing is removed) or name a new folder"
        )


def _no_links(out: Path, wrote: set[str]) -> None:
    """Refuses a symlink on any path the export writes or under out/static, and a hard-linked file."""
    for rel in (*wrote, MARKER):
        p = out
        for part in Path(rel).parts:
            p = p / part
            if p.is_symlink():
                raise ExportError(f"{p} is a symlink; the export writes through none")
        if p.is_file() and p.stat().st_nlink > 1:
            raise ExportError(f"{p} has another hard link; the export writes through none")
    for root, dirs, files in os.walk(out / "static", followlinks=False):
        for name in dirs + files:
            if (Path(root) / name).is_symlink():
                raise ExportError(
                    f"{Path(root) / name} is a symlink; the export writes through none"
                )


def _overlaps(out: Path, src: Path) -> bool:
    """`out` equals, contains or sits inside `src`, by inode, so case and links don't matter."""

    def same(a: Path, b: Path) -> bool:
        return a.exists() and b.exists() and os.path.samefile(a, b)

    outs = [out.absolute(), *out.absolute().parents]
    return any(same(p, src) for p in outs) or any(same(q, out) for q in (src, *src.parents))


def _guard(out: Path, static: Path, force: bool, wrote: set[str]) -> None:
    """Refuses a folder the export did not make, one overlapping the pages, and links."""
    src = static.resolve().parent
    if _overlaps(out, src):
        raise ExportError(f"{out} overlaps the page source {src}")
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        raise ExportError(f"{out} is not a folder")
    if (out / MARKER).is_symlink():
        raise ExportError(f"{out / MARKER} is a symlink")
    if (out / MARKER).is_file():
        _check_marker(out, wrote, force)
    elif out.exists() and any(out.iterdir()) and not force:
        raise ExportError(
            f"{out} is not empty and has no {MARKER}; name an empty folder or pass --force"
        )
    _no_links(out, wrote)


def _age(doc: dict[str, Any], now: datetime | None, stale_ok: bool) -> None:
    """Refuses a snapshot older than the App's MAX_AGE, or ahead by more than its SKEW."""
    if now is None or stale_ok:
        return
    try:
        built = datetime.fromisoformat(str(doc["built_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as err:
        raise ExportError("snapshot has no readable built_at") from err
    if built.tzinfo is None:
        raise ExportError(f"snapshot built_at {doc['built_at']} has no UTC offset")
    rule = app_bridge.module("integrity")
    if now - built > rule.MAX_AGE:
        mins = (now - built).total_seconds() / 60
        raise ExportError(
            f"snapshot is {mins:.0f} min old, over {rule.MAX_AGE}; pass --stale-ok to export it"
        )
    if built - now > rule.SKEW:
        raise ExportError(f"snapshot built_at {doc['built_at']} is ahead of this clock")


def _dir(top: int, rel: str) -> int:
    """A handle on `rel`'s folder under `top`, made if missing, never through a symlink."""
    fd = os.dup(top)
    for part in rel.split("/")[:-1]:
        try:
            os.mkdir(part, 0o755, dir_fd=fd)
        except FileExistsError:
            pass
        try:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        finally:
            os.close(fd)
        fd = nxt
    return fd


def _put(top: int, rel: str, data: bytes) -> None:
    """Writes a temp file in the checked folder and renames it over the name."""
    name = rel.split("/")[-1]
    fd = _dir(top, rel)
    tmp = f".{name}.{secrets.token_hex(6)}.tmp"
    try:
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
                raise ExportError(f"{rel} is not a plain file with one link")
        except FileNotFoundError:
            pass
        w = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
        with os.fdopen(w, "wb") as f:
            f.write(data)
        os.rename(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=fd)
        raise
    finally:
        os.close(fd)


def write(
    doc: dict[str, Any],
    out: Path,
    static: Path = APP_STATIC,
    *,
    force: bool = False,
    now: datetime | None = None,
    stale_ok: bool = False,
) -> Path:
    """Checks the document, then writes index.html, static/ and snapshot.js under `out`.

    Only the export's own files are written, never through a symlink; nothing is removed.
    """
    if not snapshot.verify(doc):
        raise ExportError("snapshot failed its sha256 or schema check")
    _age(doc, now, stale_ok)
    if not (static / "app.js").is_file():
        raise ExportError(f"{static} has no app.js; run from the repository checkout")
    files = _files(static)
    wrote = {*files, "snapshot.js"}
    _guard(out, static, force, wrote)
    body = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    try:
        out.mkdir(parents=True, exist_ok=True)
        top = os.open(out, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for rel, src in files.items():
                _put(top, rel, src.read_bytes())
            _put(top, "snapshot.js", f"window.WS_SNAPSHOT = {body};\n".encode())
            _put(top, MARKER, (json.dumps(sorted(wrote), indent=1) + "\n").encode())
        finally:
            os.close(top)
    except OSError as err:
        raise ExportError(f"{out}: {err}") from err
    return out / "index.html"
