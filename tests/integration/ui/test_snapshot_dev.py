"""The snapshot the dev job wrote meets the contract and passes the App's own sha256 check."""

import json
import sys
from pathlib import Path

from tests.contract.snapshot_contract import problems
from tests.integration.conftest import PROFILE

APP = Path(__file__).resolve().parents[3] / "app"


def test_dev_snapshot_meets_the_contract(n):
    from databricks.sdk import WorkspaceClient

    path = f"{n.volume_path('signals', 'ui')}/snapshot.json"
    doc = json.loads(WorkspaceClient(profile=PROFILE).files.download(path).contents.read())
    sys.path.insert(0, str(APP))
    from ws_ui import source

    assert source._verify(doc)
    assert problems(doc) == []
