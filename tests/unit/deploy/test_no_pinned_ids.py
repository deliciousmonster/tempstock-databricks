"""No workspace id is pinned in the bundle, the code or the tests: each is looked up or passed."""

import re
import subprocess

from .conftest import REPO

# A 16-hex warehouse id, or a UUID principal id (runner, agent, App).
PINNED = re.compile(
    r"\b[0-9a-f]{16}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
TRACKED = ("databricks.yml", "resources", "app", "src", "tests/integration")


def test_nothing_tracked_pins_a_workspace_id():
    files = subprocess.run(
        ["git", "ls-files", *TRACKED], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    hits = [
        f"{f}: {m}"
        for f in files
        if f.endswith((".yml", ".yaml", ".py", ".json"))
        for m in PINNED.findall((REPO / f).read_text(encoding="utf-8", errors="replace"))
    ]
    assert hits == []


def test_the_bundle_has_no_second_app_bundle_and_no_prefix_default():
    assert not (REPO / "app" / "databricks.yml").exists()
    text = (REPO / "databricks.yml").read_text(encoding="utf-8")
    block = text.split("  schema_prefix:", 1)[1].split("\n  app_prefix:", 1)[0]
    assert "\n    default:" not in block
