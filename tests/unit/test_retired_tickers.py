"""No retired ticker survives the stock switch outside the recorded vendor fixtures that name it."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RETIRED = re.compile(r"(?i)(?<![a-z0-9])(cdw|xlk)(?![a-z0-9])")
SCANNED = (
    "src", "app", "resources", "aibi", "scripts", "docs",
    "README.md", "pyproject.toml", "databricks.yml", "tests",
)  # fmt: skip
ALLOWED = {"tests/unit/test_retired_tickers.py"}


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--", *SCANNED], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [p for p in out.splitlines() if (ROOT / p).is_file()]


def test_no_retired_ticker_outside_the_allowlist():
    hits = []
    for p in _tracked():
        if p in ALLOWED or p.startswith(("tests/fixtures/", "app/static/vendor/")):
            continue
        text = (ROOT / p).read_text(encoding="utf-8", errors="ignore")
        hits += [f"{p}:{text.count(chr(10), 0, m.start()) + 1}" for m in RETIRED.finditer(text)]
    assert hits == []


def test_the_pattern_finds_what_it_must():
    assert RETIRED.findall("CDW close, xlk_ret_5d, cdw-daily") == ["CDW", "xlk", "cdw"]
    assert not RETIRED.search("abcdwx xlkz")
