"""The export writes the pages and `window.WS_SNAPSHOT`, and writes nothing for a document that fails its check."""

from __future__ import annotations

import json

import pytest

from tests.ui.export_fixture import document
from weather_signal import cli
from weather_signal.serving import export


def test_writes_the_pages_and_the_snapshot(tmp_path):
    doc = document()
    src = tmp_path / "snapshot.json"
    src.write_text(json.dumps(doc), encoding="utf-8")
    out = tmp_path / "out"
    assert cli.main(["ui-export", "--out", str(out), "--from", str(src), "--stale-ok"]) == 0
    assert (out / "index.html").is_file() and (out / "static" / "app.js").is_file()
    assert (out / "static" / "integrity.js").is_file() and (
        out / "static" / "vendor" / "uPlot.iife.min.js"
    ).is_file()
    js = (out / "snapshot.js").read_text(encoding="utf-8")
    assert (
        js.startswith("window.WS_SNAPSHOT = ") and json.loads(js[21:].rstrip().rstrip(";")) == doc
    )
    html = (out / "index.html").read_text(encoding="utf-8")
    assert html.index("integrity.js") < html.index("snapshot.js") < html.index("app.js")


def test_a_tampered_snapshot_writes_nothing(tmp_path):
    doc = document()
    doc["payload"]["outlook"][0]["shown_action"] = "sell"  # the fixture's is "buy"
    with pytest.raises(export.ExportError, match="sha256"):
        export.write(doc, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_a_folder_the_export_did_not_make_is_refused_and_left_alone(tmp_path):
    """`--out` over a site deleted its static/ and replaced its index.html."""
    site = tmp_path / "site"
    (site / "static").mkdir(parents=True)
    (site / "static" / "mine.css").write_text("precious", encoding="utf-8")
    (site / "index.html").write_text("<h1>mine</h1>", encoding="utf-8")
    with pytest.raises(export.ExportError, match="not empty"):
        export.write(document(), site)
    assert (site / "static" / "mine.css").read_text(encoding="utf-8") == "precious"
    assert (site / "index.html").read_text(encoding="utf-8") == "<h1>mine</h1>"
    assert sorted(p.name for p in site.rglob("*")) == ["index.html", "mine.css", "static"]


def test_force_writes_beside_what_is_there_and_removes_none_of_it(tmp_path):
    site = tmp_path / "site"
    (site / "static").mkdir(parents=True)
    (site / "static" / "mine.css").write_text("precious", encoding="utf-8")
    export.write(document(), site, force=True)
    assert (site / "static" / "mine.css").read_text(encoding="utf-8") == "precious"
    assert (site / "static" / "app.js").is_file() and (site / export.MARKER).is_file()


def test_the_page_source_is_refused(tmp_path):
    static = tmp_path / "app" / "static"
    static.mkdir(parents=True)
    for name in ("app.js", "index.html"):
        (static / name).write_text(name, encoding="utf-8")
    for out in (tmp_path / "app", static, tmp_path):
        with pytest.raises(export.ExportError, match="overlaps"):
            export.write(document(), out, static, force=True)
    assert sorted(p.name for p in static.iterdir()) == ["app.js", "index.html"]


def _marked(tmp_path):
    out = tmp_path / "out"
    export.write(document(), out)
    return out


@pytest.mark.parametrize(
    "entry", ["../victim/important.txt", "ABS", "static/../../victim/important.txt"]
)
def test_a_forged_marker_is_refused_and_removes_nothing(tmp_path, entry):
    """Marker entries were unlinked as `out / rel`, so `..` and absolute paths escaped."""
    victim = tmp_path / "victim" / "important.txt"
    victim.parent.mkdir()
    victim.write_text("SECRET", encoding="utf-8")
    out = _marked(tmp_path)
    entry = str(victim) if entry == "ABS" else entry
    (out / export.MARKER).write_text(json.dumps(["index.html", entry]), encoding="utf-8")
    for force in (False, True):
        with pytest.raises(export.ExportError, match="outside the folder"):
            export.write(document(), out, force=force)
    assert victim.read_text(encoding="utf-8") == "SECRET"


def test_a_former_file_needs_force_and_is_left_alone(tmp_path):
    static = tmp_path / "src" / "static"
    static.mkdir(parents=True)
    for name in ("app.js", "index.html", "old.js"):
        (static / name).write_text(name, encoding="utf-8")
    out = tmp_path / "out"
    export.write(document(), out, static)
    (static / "old.js").unlink()
    with pytest.raises(export.ExportError, match="no longer writes.*--force"):
        export.write(document(), out, static)
    export.write(document(), out, static, force=True)
    assert (out / "static" / "old.js").read_text(encoding="utf-8") == "old.js"
    assert "static/old.js" not in json.loads((out / export.MARKER).read_text(encoding="utf-8"))


@pytest.mark.parametrize("link", ["index.html", "static", "static/vendor", "snapshot.js"])
def test_a_symlink_on_a_written_path_is_refused(tmp_path, link):
    """`write_bytes` followed a symlink at index.html or under static/."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "target.txt").write_text("mine", encoding="utf-8")
    out = _marked(tmp_path)
    path = out / link
    if path.is_dir():
        import shutil

        shutil.rmtree(path)
    else:
        path.unlink()
    path.symlink_to(victim / "target.txt" if "." in link else victim)
    with pytest.raises(export.ExportError, match="symlink"):
        export.write(document(), out)
    assert sorted(p.name for p in victim.iterdir()) == ["target.txt"]
    assert (victim / "target.txt").read_text(encoding="utf-8") == "mine"


@pytest.mark.parametrize("body", ["{not", '"abc"', "[1, 2]"])
def test_a_corrupt_marker_says_why_and_exits_1(tmp_path, capsys, body):
    """A corrupt marker printed a traceback."""
    out = _marked(tmp_path)
    (out / export.MARKER).write_text(body, encoding="utf-8")
    src = tmp_path / "snapshot.json"
    src.write_text(json.dumps(document()), encoding="utf-8")
    assert cli.main(["ui-export", "--out", str(out), "--from", str(src), "--stale-ok"]) == 1
    assert "not exported:" in capsys.readouterr().out


def test_a_file_as_out_says_why_and_exits_1(tmp_path, capsys):
    (tmp_path / "f").write_text("x", encoding="utf-8")
    src = tmp_path / "snapshot.json"
    src.write_text(json.dumps(document()), encoding="utf-8")
    assert (
        cli.main(["ui-export", "--out", str(tmp_path / "f"), "--from", str(src), "--stale-ok"]) == 1
    )
    assert "not exported:" in capsys.readouterr().out and (tmp_path / "f").read_text() == "x"


def test_a_snapshot_older_than_the_bound_is_refused_unless_asked(tmp_path):
    """The export had no age bound."""
    from datetime import UTC, datetime, timedelta

    from tests.ui.export_fixture import NOW

    built = NOW.replace(tzinfo=UTC)
    export.write(document(), tmp_path / "a", now=built + timedelta(minutes=75))
    with pytest.raises(export.ExportError, match="min old"):
        export.write(document(), tmp_path / "b", now=built + timedelta(minutes=76))
    assert not (tmp_path / "b").exists()
    export.write(document(), tmp_path / "b", now=datetime(2030, 1, 1, tzinfo=UTC), stale_ok=True)


def test_the_cli_says_why_and_exits_1(tmp_path, capsys):
    (tmp_path / "x").write_text("x", encoding="utf-8")
    src = tmp_path / "snapshot.json"
    src.write_text(json.dumps(document()), encoding="utf-8")
    assert cli.main(["ui-export", "--out", str(tmp_path), "--from", str(src)]) == 1
    assert "not exported:" in capsys.readouterr().out


def test_a_hung_download_ends_the_process_within_its_bound(tmp_path):
    """The bound raised at 2 s but the process waited for the read to finish."""
    script = tmp_path / "hang.py"
    script.write_text(
        "import sys, time, types\n"
        "class W:\n"
        "    def __init__(self, **kw):\n"
        "        self.files = types.SimpleNamespace(download=lambda p: time.sleep(60))\n"
        "sdk = types.ModuleType('databricks.sdk'); sdk.WorkspaceClient = W\n"
        "sys.modules['databricks.sdk'] = sdk\n"
        "from weather_signal.serving import export\n"
        "try:\n"
        "    export.download('/Volumes/x/snapshot.json', None, bound_s=1)\n"
        "except export.ExportError as err:\n"
        "    print(err)\n",
        encoding="utf-8",
    )
    import subprocess
    import sys
    import time

    t = time.monotonic()
    r = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=30, check=True
    )
    assert "no answer in 1 s" in r.stdout and time.monotonic() - t < 15


@pytest.mark.parametrize("rel", ["index.html", "static/app.js"])
def test_a_hard_linked_file_is_refused_and_its_twin_kept(tmp_path, rel):
    """`is_symlink` passed a hard link, and write_bytes overwrote the twin."""
    twin = tmp_path / "twin.txt"
    twin.write_text("ORIG", encoding="utf-8")
    out = _marked(tmp_path)
    (out / rel).unlink()
    (out / rel).hardlink_to(twin)
    with pytest.raises(export.ExportError, match="hard link"):
        export.write(document(), out)
    assert twin.read_text(encoding="utf-8") == "ORIG"


def test_a_symlink_swapped_in_after_the_check_is_not_written_through(tmp_path, monkeypatch):
    """`static/` replaced by a symlink between the check and the writes."""
    import shutil

    victim = tmp_path / "victim"
    victim.mkdir()
    out = _marked(tmp_path)
    check = export._no_links

    def swap(o, w):
        check(o, w)
        shutil.rmtree(out / "static")
        (out / "static").symlink_to(victim)

    monkeypatch.setattr(export, "_no_links", swap)
    with pytest.raises(export.ExportError):
        export.write(document(), out)
    assert list(victim.iterdir()) == []


def test_a_case_swapped_page_source_is_refused(tmp_path):
    """`APP` reached app/ on a case-insensitive disk."""
    probe = tmp_path / "Probe"
    probe.mkdir()
    if not (tmp_path / "PROBE").exists():
        pytest.skip("tmp is case-sensitive here")
    static = tmp_path / "app" / "static"
    static.mkdir(parents=True)
    for name in ("app.js", "index.html"):
        (static / name).write_text(name, encoding="utf-8")
    for out in (tmp_path / "APP", tmp_path / "APP" / "STATIC", tmp_path / "App" / "x"):
        with pytest.raises(export.ExportError, match="overlaps"):
            export.write(document(), out, static, force=True)
    assert sorted(p.name for p in (tmp_path / "app").iterdir()) == ["static"]


@pytest.mark.parametrize("built_at", ["2026-11-18T12:15:00", "2026-11-18T12:30:00Z"])
def test_a_naive_or_future_built_at_says_why(tmp_path, built_at):
    """A built_at without an offset raised TypeError; one ahead of the clock passed."""
    from datetime import UTC, datetime

    doc = document()
    doc["built_at"] = built_at
    with pytest.MonkeyPatch.context() as m:
        m.setattr(export.snapshot, "verify", lambda d: True)  # the digest is not a MAC
        with pytest.raises(export.ExportError, match="no UTC offset|ahead"):
            export.write(doc, tmp_path / "o", now=datetime(2026, 11, 18, 12, 20, tzinfo=UTC))
