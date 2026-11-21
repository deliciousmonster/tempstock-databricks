"""The `databricks` CLI as bootstrap calls it: argv as data, stdin closed unless given, JSON parsed."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol


class Unknown(Exception):
    """A probe that did not answer; the run stops with nothing written."""


class Stop(Exception):
    """A refusal with its exit code and the line the operator reads."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


ERROR_LINES = ("Error:", "API error_code:", "API message:", "HTTP Status:")


@dataclass(frozen=True)
class Result:
    rc: int
    out: str
    err: str

    def tail(self, lines: int = 8) -> str:
        """The CLI's `Error:` lines wherever they fell, then the last lines of stderr and of stdout."""
        both = (self.err + "\n" + self.out).splitlines()
        errors = [x for x in both if x.lstrip().startswith(ERROR_LINES)][:lines]
        parts = ["\n".join(errors)] if errors else []
        for name, text in (("stderr", self.err), ("stdout", self.out)):
            if text.strip():
                parts.append(f"{name}:\n" + "\n".join(text.strip().splitlines()[-lines:]))
        return "\n".join(parts)


class Runner(Protocol):
    def __call__(self, argv: Sequence[str], stdin: str | None = None) -> Result: ...


def subprocess_runner(timeout: float = 3600.0) -> Runner:
    def run(argv: Sequence[str], stdin: str | None = None) -> Result:
        p = subprocess.run(
            list(argv),
            input=stdin if stdin is not None else "",
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return Result(p.returncode, p.stdout, p.stderr)

    return run


def call_json(run: Runner, argv: Sequence[str]) -> Any:
    """`-o json` output; a non-zero exit or unparsable output is Unknown."""
    r = run([*argv, "-o", "json"])
    if r.rc != 0:
        raise Unknown(f"{' '.join(argv[:3])}: exit {r.rc}: {r.tail(3)}")
    try:
        return json.loads(r.out or "null")
    except json.JSONDecodeError as err:
        raise Unknown(f"{' '.join(argv[:3])}: output is not JSON") from err


# The CLI's own error line for an absent object ("Error: Path (...) doesn't exist.", "Error: Job 1
# does not exist."), or the API's error code; never a bare "404", which a 5xx body's request id can hold.
NOT_FOUND = re.compile(
    r"^Error: .*\b(does not exist|doesn't exist|not found)\b|\b(RESOURCE_DOES_NOT_EXIST|NOT_FOUND)\b",
    re.IGNORECASE | re.MULTILINE,
)


def not_found(r: Result) -> bool:
    """A failed call whose text says the thing is absent; the CLI words it more than one way."""
    return r.rc != 0 and NOT_FOUND.search(r.err + r.out) is not None
