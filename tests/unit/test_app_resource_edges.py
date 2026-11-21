"""An App names bundle objects by `${resources.}` reference, the only thing that orders its create.

Measured 2026-11-19: a var-built `securable_full_name` gave the direct engine no edge, and the App's
create raced the ui volume's and failed 404.
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
REF = re.compile(r"\$\{resources\.(\w+)\.(\w+)\.\w+\}")


def _bundle_resources():
    out: dict[str, set[str]] = {}
    for f in sorted((ROOT / "resources").glob("*.yml")):
        for kind, items in (yaml.safe_load(f.read_text()).get("resources") or {}).items():
            out.setdefault(kind, set()).update(items)
    return out


def _apps():
    doc = yaml.safe_load((ROOT / "resources" / "weather_signal_ui.app.yml").read_text())
    for target, body in doc["targets"].items():
        for key, app in ((body.get("resources") or {}).get("apps") or {}).items():
            yield f"{target}.{key}", app


def _names(app):
    """Each string in the App that names a UC object: its securables and its /Volumes paths."""
    for r in app.get("resources") or []:
        if "uc_securable" in r:
            yield r["uc_securable"]["securable_full_name"]
    for e in (app.get("config") or {}).get("env") or []:
        if str(e.get("value", "")).startswith("/Volumes/"):
            yield e["value"]


APPS = list(_apps())


@pytest.mark.parametrize(("where", "app"), APPS, ids=[w for w, _ in APPS])
def test_every_uc_name_in_an_app_is_built_from_resource_references(where, app):
    have = _bundle_resources()
    names = list(_names(app))
    assert names
    for name in names:
        assert "${var." not in name, f"{where}: {name} is var-built; it orders nothing"
        refs = REF.findall(name)
        assert refs, f"{where}: {name} names no bundle resource"
        assert all(key in have.get(kind, ()) for kind, key in refs), name
