"""U2 in the page's script: Node runs `app/static/integrity.js` on the fixture, equal case by case."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.ui.test_integrity import FIXTURE, ROOT, case_inputs, integrity

NODE = shutil.which("node")
RUNNER = """
const {integrityState} = require(process.argv[1]);
const cases = JSON.parse(require("fs").readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(cases.map(([row, now]) => integrityState(row, now))));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("tz", ["UTC", "Asia/Tokyo"])
def test_js_equals_python_case_by_case(tz):
    inputs = [case_inputs(c) for c in FIXTURE["cases"]]
    out = subprocess.run(
        [str(NODE), "-e", RUNNER, str(ROOT / "app/static/integrity.js")],
        input=json.dumps(inputs),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env={**os.environ, "TZ": tz},
    )
    js = json.loads(out.stdout)
    py = [integrity.state(row, now) for row, now in inputs]
    names = [c["name"] for c in FIXTURE["cases"]]
    assert [(n, j) for n, j, p in zip(names, js, py, strict=True) if j != p] == []
    assert js == [c["want"] for c in FIXTURE["cases"]]


def test_runner_path_exists():
    assert Path(ROOT / "app/static/integrity.js").is_file()
