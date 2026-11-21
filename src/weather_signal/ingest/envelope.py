"""The landed envelope: the response as received, its request without credentials, and its key."""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from weather_signal.ingest.fetch import RawResponse
from weather_signal.redaction import strip_credentials


def landing_key(source: str, endpoint: str, params: Mapping[str, str], slot: str) -> str:
    """sha256 of the request without credentials and its slot; two writers of one slot agree."""
    canon = json.dumps(
        {"source": source, "endpoint": endpoint, "params": strip_credentials(params), "slot": slot},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode()).hexdigest()


def landing_path(root: str, source: str, slot_time: datetime, key: str) -> str:
    t = slot_time.astimezone(UTC)
    return f"{root}/{source}/{t:%Y/%m/%d/%H}/{key}.json.gz"


def build(
    source: str,
    endpoint: str,
    params: Mapping[str, str],
    slot: str,
    resp: RawResponse,
    host: str,
) -> dict[str, Any]:
    body_text = resp.body.decode("utf-8")
    return {
        "source": source,
        "request": {"endpoint": endpoint, "params": strip_credentials(params)},
        "slot": slot,
        "host": host,
        "fetched_at": resp.fetched_at.astimezone(UTC).isoformat(),
        "http_status": resp.status,
        "ratelimit_cost": _float(resp.headers.get("X-RateLimit-Cost")),
        "body_sha256": hashlib.sha256(resp.body).hexdigest(),
        "body_text": body_text,
    }


def encode(envelope: Mapping[str, Any]) -> bytes:
    return gzip.compress(json.dumps(envelope, separators=(",", ":")).encode(), mtime=0)


def decode(data: bytes) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(gzip.decompress(data))
    return out


def _float(v: str | None) -> float | None:
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None
