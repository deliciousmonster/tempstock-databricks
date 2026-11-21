"""The seed: the archive verifies locally, uploads create only, a different size is never overwritten."""

import gzip
import hashlib
import json

import pytest

from tests.unit.fakes import FakeStore
from weather_signal.deploy import seed
from weather_signal.deploy.cli import Stop, Unknown

VOL = "/Volumes/workspace/dev_raw/raw"


def envelope(source, ticker):
    req = (
        {"endpoint": "https://www.alphavantage.co/query", "params": {"symbol": ticker}}
        if source == "alphavantage"
        else {
            "endpoint": f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            "params": {},
        }
    )
    return gzip.compress(json.dumps({"source": source, "request": req}).encode())


def archive(tmp_path, files):
    out = []
    for rel, data in files.items():
        p = tmp_path / "raw" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        out.append(
            {"path": f"raw/{rel}", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    (tmp_path / "manifest.json").write_text(json.dumps({"files": out}), encoding="utf-8")
    return tmp_path


FILES = {
    "open-meteo/2026/11/01/00/a.json.gz": b"weather",
    "yahoo/2026/11/01/00/aaa.json.gz": envelope("yahoo", "AAA"),
    "yahoo/2026/11/01/00/wmt.json.gz": envelope("yahoo", "WMT"),
    "alphavantage/2026/11/01/00/spy.json.gz": envelope("alphavantage", "SPY"),
    "alphavantage/2026/11/01/00/xlp.json.gz": envelope("alphavantage", "XLP"),
}


def test_a_verified_archive_loads_with_paths_below_the_volume(tmp_path):
    got, notes = seed.load(archive(tmp_path, FILES))
    assert {e.path for e in got} == set(FILES) and notes == []


def test_a_changed_file_is_exit_2(tmp_path):
    root = archive(tmp_path, FILES)
    (root / "raw" / "open-meteo/2026/11/01/00/a.json.gz").write_bytes(b"weathe!")
    with pytest.raises(Stop) as e:
        seed.load(root)
    assert e.value.code == 2 and "bytes differ" in str(e.value)


def test_a_partial_archive_loads_what_is_there_and_says_what_is_not(tmp_path):
    root = archive(tmp_path, FILES)
    (root / "raw" / "open-meteo/2026/11/01/00/a.json.gz").unlink()
    (root / "raw" / "open-meteo/2026/11/02/00/b.json.gz").parent.mkdir(parents=True)
    (root / "raw" / "open-meteo/2026/11/02/00/b.json.gz").write_bytes(b"landed later")
    got, notes = seed.load(root)
    paths = {e.path for e in got}
    assert "open-meteo/2026/11/01/00/a.json.gz" not in paths
    assert "open-meteo/2026/11/02/00/b.json.gz" in paths
    assert notes == [
        "1 manifest files not on disk, left out (first: raw/open-meteo/2026/11/01/00/a.json.gz)",
        "1 files not in the manifest, hashed here",
    ]


def test_coverage_merges_slots_into_spans_per_source(tmp_path):
    def era5(slot):
        return gzip.compress(json.dumps({"source": "open-meteo-era5", "slot": slot}).encode())

    files = {
        "open-meteo-era5/x/1.json.gz": era5("1991-01-01..1991-06-30"),
        "open-meteo-era5/x/2.json.gz": era5("1991-07-01..1991-12-31"),
        "open-meteo-era5/x/3.json.gz": era5("2013-06-27..2013-12-31"),
        "yahoo/x/wmt.json.gz": envelope("yahoo", "WMT"),
    }
    root = archive(tmp_path, files)
    got, _ = seed.load(root)
    assert seed.coverage(root, got) == [
        "open-meteo-era5: 3 files, 1991-01-01..1991-12-31, 2013-06-27..2013-12-31",
        "yahoo: 1 files",
    ]
    assert seed.absent_tickers(root, got, ("WMT", "SPY")) == ["SPY"]


def test_price_envelopes_of_tickers_outside_config_are_left_out(tmp_path):
    root = archive(tmp_path, FILES)
    got, skipped = seed.kept(root, seed.load(root)[0], tickers=("AAA", "SPY"))
    assert {e.path.rsplit("/", 1)[1] for e in got} == {"a.json.gz", "aaa.json.gz", "spy.json.gz"}
    assert skipped == {"WMT": 1, "XLP": 1}
    line = seed.skipped_line(skipped)
    assert line == "2 price envelopes for tickers outside config skipped: WMT, XLP"


def test_a_price_envelope_without_a_ticker_is_unknown():
    with pytest.raises(Unknown):
        seed.ticker({"source": "alphavantage", "request": {"params": {}}})


def test_only_the_missing_files_are_uploaded_and_none_overwritten(tmp_path):
    root = archive(tmp_path, FILES)
    want = seed.load(root)[0]
    have = {"open-meteo/2026/11/01/00/a.json.gz": 7}
    todo = seed.missing(want, have, VOL)
    assert len(todo) == len(FILES) - 1
    store = FakeStore()
    assert seed.upload(root, todo, store, VOL) == len(todo)
    assert f"{VOL}/open-meteo/2026/11/01/00/a.json.gz" not in store.puts


def test_a_file_at_another_size_is_exit_1_with_the_remove_line(tmp_path):
    want = seed.load(archive(tmp_path, FILES))[0]
    with pytest.raises(Stop) as e:
        seed.missing(want, {"open-meteo/2026/11/01/00/a.json.gz": 99}, VOL)
    assert e.value.code == 1 and "databricks fs rm dbfs:" in str(e.value)


def test_an_upload_that_does_not_answer_is_unknown(tmp_path):
    root = archive(tmp_path, FILES)
    store = FakeStore()
    store.unknown_puts.add(f"{VOL}/open-meteo/2026/11/01/00/a.json.gz")
    with pytest.raises(Unknown):
        seed.upload(root, seed.load(root)[0], store, VOL)


def test_only_raw_and_the_manifest_are_read(tmp_path):
    """The data folder also holds the fetcher's fetch/ and state/; neither is seeded."""
    root = archive(tmp_path, FILES)
    doc = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    doc["files"].append({"path": "state/ledger.json", "bytes": 2, "sha256": "x"})
    (root / "manifest.json").write_text(json.dumps(doc), encoding="utf-8")
    (root / "state").mkdir()
    (root / "state" / "ledger.json").write_text("{}", encoding="utf-8")
    (root / "fetch").mkdir()
    (root / "fetch" / "log.txt").write_text("x", encoding="utf-8")
    got, notes = seed.load(root)
    assert {e.path for e in got} == set(FILES) and notes == []
