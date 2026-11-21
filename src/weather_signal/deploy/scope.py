"""The one frozen scope every phase, bundle command and SQL string is built from."""

from __future__ import annotations

import re
from dataclasses import dataclass

from weather_signal.config import Names

TARGETS = ("dev", "scratch", "prod")
FIXED_PREFIX = {"dev": "dev_", "prod": ""}
SCRATCH_PREFIX = re.compile(r"t[0-9]{1,3}_")
CATALOG = re.compile(r"[a-z_][a-z0-9_]{0,254}")
# The raw volume's schema; the rename commit changes this one name with the rest.
RAW_SCHEMA = "raw"


class ScopeError(ValueError):
    """A target, prefix, profile or catalog the bootstrap refuses before any call."""


@dataclass(frozen=True)
class Scope:
    profile: str
    target: str
    prefix: str
    catalog: str = "workspace"

    @property
    def app_prefix(self) -> str:
        return self.prefix.replace("_", "-")

    @property
    def names(self) -> Names:
        return Names(self.catalog, self.prefix)

    @property
    def app_name(self) -> str:
        return f"{self.app_prefix}weather-signal-ui"

    @property
    def raw_volume(self) -> str:
        return self.names.volume_path(RAW_SCHEMA, "raw")

    def fq(self, schema: str, name: str) -> str:
        """A backquoted three-part name; the only way SQL here names an object."""
        return f"`{self.catalog}`.`{self.prefix}{schema}`.`{name}`"

    def bundle_args(self, warehouse_id: str, pause: str = "PAUSED") -> list[str]:
        return [
            "--profile",
            self.profile,
            "-t",
            self.target,
            "--var",
            f"schema_prefix={self.prefix}",
            "--var",
            f"app_prefix={self.app_prefix}",
            "--var",
            f"warehouse_id={warehouse_id}",
            "--var",
            f"catalog={self.catalog}",
            "--var",
            f"schedule_pause={pause}",
        ]


def build(profile: str | None, target: str, prefix: str | None, catalog: str) -> Scope:
    if not profile:
        raise ScopeError("--profile is required; there is no default profile")
    if target not in TARGETS:
        raise ScopeError(f"--target {target!r} is refused; bootstrap takes {' or '.join(TARGETS)}")
    if not CATALOG.fullmatch(catalog):
        raise ScopeError(f"--catalog {catalog!r} is not a plain catalog name")
    if target in FIXED_PREFIX:
        fixed = FIXED_PREFIX[target]
        if prefix not in (None, fixed):
            raise ScopeError(f"--target {target} takes no --schema-prefix; its prefix is {fixed!r}")
        return Scope(profile, target, fixed, catalog)
    if prefix is None or not SCRATCH_PREFIX.fullmatch(prefix):
        raise ScopeError(f"--target scratch needs --schema-prefix tN_ (got {prefix!r})")
    return Scope(profile, target, prefix, catalog)
