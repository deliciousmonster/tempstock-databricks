"""Files API create-if-absent: the upload is the compare-and-set.

Classified on status alone; a 409's body is plain text as often as JSON, so it is never parsed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import quote

import requests

Outcome = Literal["created", "exists", "unknown"]
Presence = Literal["present", "absent", "unknown"]
TIMEOUT = (30.0, 60.0)


class Store(Protocol):
    """What landing needs from a volume: create-if-absent and a presence check."""

    def put_if_absent(self, path: str, data: bytes) -> Outcome: ...
    def exists(self, path: str) -> Presence: ...


class ReadStore(Store, Protocol):
    """A Store that also reads a file back: None when absent, `Unreadable` on anything else."""

    def read(self, path: str) -> bytes | None: ...


class HttpClient(Protocol):
    def put(
        self, url: str, data: bytes, headers: Mapping[str, str], timeout: tuple[float, float]
    ) -> int: ...
    def head(self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]) -> int: ...
    def delete(self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]) -> int: ...
    def get(
        self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]
    ) -> tuple[int, bytes]: ...


class RequestsClient:
    def __init__(self) -> None:
        self.s = requests.Session()

    def put(
        self, url: str, data: bytes, headers: Mapping[str, str], timeout: tuple[float, float]
    ) -> int:
        return self.s.put(url, data=data, headers=dict(headers), timeout=timeout).status_code

    def head(self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]) -> int:
        return self.s.head(url, headers=dict(headers), timeout=timeout).status_code

    def get(
        self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]
    ) -> tuple[int, bytes]:
        r = self.s.get(url, headers=dict(headers), timeout=timeout)
        return r.status_code, r.content

    def delete(self, url: str, headers: Mapping[str, str], timeout: tuple[float, float]) -> int:
        return self.s.delete(url, headers=dict(headers), timeout=timeout).status_code


@dataclass
class Files:
    host: str
    auth: Callable[[], Mapping[str, str]]
    client: HttpClient

    @classmethod
    def from_sdk(cls, profile: str | None = None) -> Files:
        from databricks.sdk.core import Config

        cfg = Config(profile=profile) if profile else Config()
        return cls(cfg.host.rstrip("/"), cfg.authenticate, RequestsClient())

    def _url(self, path: str, query: str = "") -> str:
        return f"{self.host}/api/2.0/fs/files{quote(path)}{query}"

    def put_if_absent(self, path: str, data: bytes) -> Outcome:
        try:
            status = self.client.put(
                self._url(path, "?overwrite=false"),
                data,
                {**self.auth(), "Content-Type": "application/octet-stream"},
                TIMEOUT,
            )
        except (requests.RequestException, OSError):
            return "unknown"
        if 200 <= status < 300:
            return "created"
        return "exists" if status == 409 else "unknown"

    def exists(self, path: str) -> Presence:
        try:
            status = self.client.head(self._url(path), self.auth(), TIMEOUT)
        except (requests.RequestException, OSError):
            return "unknown"
        if 200 <= status < 300:
            return "present"
        return "absent" if status == 404 else "unknown"

    def delete(self, path: str) -> bool:
        """True when the file is gone (deleted, or absent already); False on anything else."""
        try:
            status = self.client.delete(self._url(path), self.auth(), TIMEOUT)
        except (requests.RequestException, OSError):
            return False
        return 200 <= status < 300 or status == 404

    def read(self, path: str) -> bytes | None:
        """The file's bytes, None when absent; raises `Unreadable` on anything else."""
        try:
            status, body = self.client.get(self._url(path), self.auth(), TIMEOUT)
        except (requests.RequestException, OSError) as err:
            raise Unreadable(path) from err
        if status == 404:
            return None
        if 200 <= status < 300:
            return body
        raise Unreadable(path)

    def list(self, path: str) -> list[tuple[str, bool]]:
        """(name, is_directory) for each entry of a folder, [] when absent; `Unreadable` otherwise."""
        out: list[tuple[str, bool]] = []
        token = ""
        while True:
            query = f"?page_token={quote(token)}" if token else ""
            try:
                status, body = self.client.get(
                    f"{self.host}/api/2.0/fs/directories{quote(path)}{query}", self.auth(), TIMEOUT
                )
            except (requests.RequestException, OSError) as err:
                raise Unreadable(path) from err
            if status == 404:
                return out
            if not 200 <= status < 300:
                raise Unreadable(path)
            doc = json.loads(body or b"{}")
            out += [(c["name"], bool(c.get("is_directory"))) for c in doc.get("contents", [])]
            token = doc.get("next_page_token") or ""
            if not token:
                return out


class Unreadable(Exception):
    """A read that neither returned the file nor said it is absent."""
