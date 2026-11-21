"""The schemas are raw, daily, signals, app and ops; no code path names the old medallion three."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OLD = re.compile(r"(?i)(?<![a-z])(bronze|silver|gold)(?![a-z])")
CODE = (
    "src", "app", "resources", "aibi", "scripts", "tests",
    ".claude/hooks", "databricks.yml", "pyproject.toml",
)  # fmt: skip
SELF = "tests/unit/test_schema_names.py"
# Unity Catalog shows each schema's tier from its comment and properties, and only there.
CATALOG = "resources/catalog.yml"


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--", *CODE], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [p for p in out.splitlines() if p not in (SELF, CATALOG) and (ROOT / p).is_file()]


def test_no_code_path_names_an_old_schema():
    hits = []
    for p in _tracked():
        text = (ROOT / p).read_text(encoding="utf-8", errors="ignore")
        hits += [f"{p}:{text.count(chr(10), 0, m.start()) + 1}" for m in OLD.finditer(text)]
    assert hits == []


def test_no_code_path_is_named_after_an_old_schema():
    assert [p for p in _tracked() if OLD.search(p)] == []


def test_the_bundle_declares_the_five_schemas():
    text = (ROOT / "resources/catalog.yml").read_text(encoding="utf-8")
    for s in ("raw", "daily", "signals", "app", "ops"):
        assert f"name: ${{var.schema_prefix}}{s}\n" in text


def test_each_schema_states_its_medallion_tier_in_unity_catalog():
    import yaml

    schemas = yaml.safe_load((ROOT / CATALOG).read_text(encoding="utf-8"))["resources"]["schemas"]
    tiers = {"raw": "bronze", "daily": "silver", "signals": "gold", "app": "gold"}
    for key, tier in tiers.items():
        assert schemas[key]["properties"] == {"medallion_tier": tier}, key
        assert schemas[key]["comment"].startswith(f"{tier.capitalize()}: "), key
    assert "properties" not in schemas["ops"]
    text = (ROOT / CATALOG).read_text(encoding="utf-8")
    assert len(OLD.findall(text)) == 2 * len(tiers)  # each tier word in a comment and a property
