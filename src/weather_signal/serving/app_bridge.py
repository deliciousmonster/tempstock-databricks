"""The App's own rule modules (`app/ws_ui`), imported from a repository checkout.

The App runs Python 3.11 and the wheel requires 3.12, so the App cannot install the wheel; the
integrity rule and the live-row typing stay in `app/ws_ui`, their one copy. `ui-export` and the MCP
server run on the Mac from a checkout, which is the only place this import works; anywhere else it
raises `ModuleNotFoundError` naming the file it looked for.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

APP = Path(__file__).resolve().parents[3] / "app"


def module(name: str) -> ModuleType:
    if not (APP / "ws_ui" / f"{name}.py").is_file():
        raise ModuleNotFoundError(f"{APP}/ws_ui/{name}.py: run from the repository checkout")
    if str(APP) not in sys.path:
        sys.path.insert(0, str(APP))
    return importlib.import_module(f"ws_ui.{name}")
