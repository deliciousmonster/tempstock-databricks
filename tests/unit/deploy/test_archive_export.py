"""`weather-signal archive export`: a deployment's volumes to a local folder, never into data/raw."""

import hashlib
import importlib
import json
from pathlib import Path

import pytest

from weather_signal import cli
from weather_signal.deploy import seed
from weather_signal.deploy.cli import Result

RAW = "/Volumes/workspace/dev_raw/raw"
LEDGER = "/Volumes/workspace/dev_raw/ledger"
VOLS = {
    RAW: {"yahoo/2026/11/18/21/a.json.gz": 5, "open-meteo-era5/2026/11/19/05/b.json.gz": 3},
    LEDGER: {"alphavantage.jsonl": 4},
}
BYTES = {
    f"{RAW}/yahoo/2026/11/18/21/a.json.gz": b"12345",
    f"{RAW}/open-meteo-era5/2026/11/19/05/b.json.gz": b"abc",
    f"{LEDGER}/alphavantage.jsonl": b"1234",
}


def _module():
    """Imported here, so the file loads on a base without the command and its tests fail there."""
    return importlib.import_module("weather_signal.deploy.archive_export")


@pytest.fixture
def made(ctx, ws, tmp_path, monkeypatch):
    """The fake workspace's volumes behind `real_source`; the scopes it was asked for."""
    archive_export = _module()
    data = tmp_path / "data"  # the conftest's data folder, with raw/ and a manifest
    monkeypatch.setattr(archive_export, "DATA", data)
    scopes = []
    files = dict(BYTES)
    ctx.files = files  # type: ignore[attr-defined]

    def source(s):
        scopes.append(s)
        ctx.scope = s
        return archive_export.Source(ctx, files.get)

    ctx.list_volume = lambda root: VOLS.get(root, {})
    monkeypatch.setattr(archive_export, "real_source", source)
    return scopes


def export(*args):
    return cli.main(["archive", "export", "--profile", "p", *args])


def test_export_writes_raw_ledger_and_a_manifest_bootstrap_seeds_from(made, tmp_path, capsys):
    out = tmp_path / "landings"
    assert export("--out", str(out)) == 0
    assert (out / "raw/yahoo/2026/11/18/21/a.json.gz").read_bytes() == b"12345"
    assert (out / "ledger/alphavantage.jsonl").read_bytes() == b"1234"
    doc = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert doc["prefix"] == "dev_" and doc["catalog"] == "workspace"
    by_path = {f["path"]: f for f in doc["files"]}
    assert by_path["raw/yahoo/2026/11/18/21/a.json.gz"] == {
        "path": "raw/yahoo/2026/11/18/21/a.json.gz", "bytes": 5, "source": "yahoo",
        "sha256": hashlib.sha256(b"12345").hexdigest(),
    }  # fmt: skip
    assert "source" not in by_path["ledger/alphavantage.jsonl"]
    entries, notes = seed.load(out)
    assert sorted(e.path for e in entries) == sorted(VOLS[RAW]) and notes == []
    said = capsys.readouterr().out
    assert f"raw: 2 files archived from {RAW}, bytes checked" in said
    assert f"ledger: 1 files archived from {LEDGER}, bytes checked" in said


def test_the_data_folder_constant_is_the_shared_archive():
    assert Path.home() / "Developer/weather-signal-data" == _module().DATA


def test_a_scratch_scope_is_refused_before_anything_is_made(tmp_path, capsys):
    """No fixture: on a base without the command, argparse refuses `archive` and this fails."""
    out = tmp_path / "x"
    assert export("--target", "scratch", "--schema-prefix", "t1_", "--out", str(out)) == 2
    assert "export takes dev or prod" in capsys.readouterr().out and not out.exists()


@pytest.mark.parametrize("where", ["data", "data/raw", "data/raw/yahoo", "link"])
def test_an_out_whose_raw_is_the_data_folders_is_refused_before_any_read(
    made, tmp_path, where, capsys
):
    (tmp_path / "link").symlink_to(tmp_path / "data")
    before = sorted((tmp_path / "data").rglob("*"))
    assert export("--out", str(tmp_path / where)) == 2
    assert "only the local fetcher writes" in capsys.readouterr().out
    assert made == [] and sorted((tmp_path / "data").rglob("*")) == before


def test_a_different_folder_inside_the_data_folder_is_allowed(made, tmp_path):
    out = tmp_path / "data/exports/dev"
    assert export("--out", str(out)) == 0
    assert (out / "raw/yahoo/2026/11/18/21/a.json.gz").is_file()
    assert sorted(p.name for p in (tmp_path / "data/raw").iterdir()) == []


def test_a_folder_holding_files_is_refused(made, tmp_path):
    out = tmp_path / "used"
    out.mkdir()
    (out / "note.txt").write_text("x", encoding="utf-8")
    assert export("--out", str(out)) == 2
    assert made == [] and [p.name for p in out.iterdir()] == ["note.txt"]


@pytest.mark.parametrize(
    "args",
    [
        ["--target", "scratch", "--schema-prefix", "t1_"],
        ["--target", "dev", "--schema-prefix", "t1_"],
        ["--target", "prod", "--schema-prefix", "dev_"],
        ["--target", "staging"],
    ],
)
def test_any_scope_outside_dev_but_an_explicit_prod_is_refused(made, tmp_path, args):
    assert export(*args, "--out", str(tmp_path / "x")) == 2
    assert made == [] and not (tmp_path / "x").exists()


def test_a_global_prefix_other_than_the_scopes_is_refused(made, tmp_path):
    argv = ["--schema-prefix", "t1_", "archive", "export", "--profile", "p"]
    assert cli.main([*argv, "--out", str(tmp_path / "x")]) == 2
    assert made == []


def test_prod_reads_prods_volumes_only_when_named(made, ctx, tmp_path):
    prod = {"/Volumes/workspace/raw/raw": {"yahoo/x.json.gz": 2}}
    ctx.list_volume = lambda root: prod.get(root, {})
    ctx.files["/Volumes/workspace/raw/raw/yahoo/x.json.gz"] = b"ok"
    out = tmp_path / "prod"
    assert export("--target", "prod", "--out", str(out)) == 0
    assert [s.prefix for s in made] == [""]
    assert (out / "raw/yahoo/x.json.gz").read_bytes() == b"ok"


def test_a_file_short_of_its_listed_size_stops_with_no_manifest(made, ctx, tmp_path):
    short = {**VOLS, LEDGER: {"alphavantage.jsonl": 9}}
    ctx.list_volume = lambda root: short.get(root, {})
    out = tmp_path / "short"
    assert export("--out", str(out)) == 1
    assert not (out / "manifest.json").exists()


def test_an_absent_volume_stops_the_export(made, ws, tmp_path, capsys):
    ws.rc["volumes read"] = Result(1, "", "Error: Volume does not exist.\n")
    assert export("--out", str(tmp_path / "none")) == 1
    assert "does not exist" in capsys.readouterr().out
    assert not (tmp_path / "none/manifest.json").exists()
