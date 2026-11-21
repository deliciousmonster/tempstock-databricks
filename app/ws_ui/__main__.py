"""`python -m ws_ui`: serve on the port Databricks Apps names (8000 by default)."""

import os

import uvicorn

uvicorn.run(
    "ws_ui.main:app", host="0.0.0.0", port=int(os.environ.get("DATABRICKS_APP_PORT", "8000"))
)
