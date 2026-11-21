"""The standard env carries the App's requirements, so `tests/app` runs in the gate."""

from __future__ import annotations

import importlib
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MODULE = {"databricks-sdk": "databricks.sdk"}


def requirements() -> list[str]:
    lines = (ROOT / "app/requirements.txt").read_text(encoding="utf-8").splitlines()
    return [re.split(r"[<>=!~ ]", ln, maxsplit=1)[0] for ln in lines if ln.strip()]


def test_every_app_requirement_imports_here():
    for name in requirements():
        importlib.import_module(MODULE.get(name, name.replace("-", "_")))
    importlib.import_module("httpx")


def test_the_app_group_is_a_default_group_and_tests_app_never_skips_itself():
    tool = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["uv"]
    assert "app" in tool["default-groups"]
    conftest = (ROOT / "tests/app/conftest.py").read_text(encoding="utf-8")
    assert "collect_ignore" not in conftest and "importorskip" not in conftest
