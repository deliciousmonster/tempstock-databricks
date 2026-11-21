"""In-memory stand-ins for the Files API store and the clock."""

from __future__ import annotations

from datetime import datetime, timedelta

from weather_signal.ingest.files import Unreadable


class FakeStore:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.unreadable: set[str] = set()
        self.unknown_puts: set[str] = set()
        self.unknown_heads: set[str] = set()
        self.puts: list[str] = []

    def put_if_absent(self, path: str, data: bytes) -> str:
        self.puts.append(path)
        if path in self.unknown_puts:
            return "unknown"
        if path in self.files:
            return "exists"
        self.files[path] = data
        return "created"

    def exists(self, path: str) -> str:
        if path in self.unknown_heads:
            return "unknown"
        return "present" if path in self.files else "absent"

    def delete(self, path: str) -> bool:
        self.files.pop(path, None)
        return True

    def read(self, path: str) -> bytes | None:
        if path in self.unreadable:
            raise Unreadable(path)
        return self.files.get(path)

    def list(self, path: str) -> list[tuple[str, bool]]:
        """A folder's entries as the Files API lists them: (name, is_directory)."""
        out: dict[str, bool] = {}
        for p in self.files:
            if p.startswith(path + "/"):
                head, _, rest = p[len(path) + 1 :].partition("/")
                out[head] = out.get(head, False) or bool(rest)
        return sorted(out.items())


class FakeClock:
    def __init__(self, t: datetime) -> None:
        self.t = t
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += timedelta(seconds=s)

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)
