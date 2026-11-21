"""The README's fresh-clone section: its prerequisites match preflight, and no key reaches history."""

import re
from pathlib import Path

from weather_signal.deploy import preflight

README = (Path(__file__).parents[2] / "README.md").read_text(encoding="utf-8")
SECTION = README.split("## Provisioning from a fresh clone", 1)[1].split("\n## ", 1)[0]


def test_the_prerequisites_name_the_cli_floor_uv_python_and_the_login():
    head = SECTION.split("Prerequisites:", 1)[1].split("\n\n", 2)[1]
    assert "CLI " + ".".join(map(str, preflight.MIN_CLI)) in head
    assert "`uv`" in head and "Python 3.12" in head and "auth login" in head and "macOS" in head


def test_no_command_writes_a_literal_key_into_the_key_file():
    literal = re.compile(r"(printf|echo)\b[^\n]*>\s*~/\.config/alphavantage/API_KEY")
    assert not literal.search(README)
    assert "pbpaste > ~/.config/alphavantage/API_KEY" in SECTION
