"""The recorded baseline and the comparison with it: counts, digests, the champion and its calls.

Every column called `path` names the raw file a row came from, under the scope's own raw volume.
The path rule strips that volume prefix before any digest and fails a value outside it (owner,
"Normalize, one confirm"). The file name is the request key, so the stripped path proves the
request and slot; the value columns prove the content.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from weather_signal.deploy.context import Sql
from weather_signal.deploy.scope import RAW_SCHEMA, Scope

# (schema, table) -> the column the cutoff applies to, or None for the whole table.
COMPARED: dict[tuple[str, str], str | None] = {
    ("daily", "metros"): None,
    ("daily", "sessions"): "session_date",
    ("daily", "weather_observed_daily"): "date",
    ("daily", "prices_daily"): "trade_date",
    ("signals", "temp_index_daily"): "date",
    ("signals", "normals"): None,
    ("signals", "forecast_index"): "known_at",
}
# Clocks of the fetch, not of the data; named, never "and the like".
EXCLUDED = {
    ("daily", "weather_observed_daily"): {"fetched_at"},
    ("daily", "prices_daily"): {"known_at"},
}
FLOATS = ("double", "float")
FORMAT = 1


class PathOutside(ValueError):
    """A `path` value outside the scope's own raw volume; the table's digest is not computed."""


def volume_prefix(scope: Scope) -> str:
    return f"/Volumes/{scope.catalog}/{scope.prefix}{RAW_SCHEMA}/"


def _column(name: str, data_type: str, prefix: str) -> str:
    c = f"`{name}`"
    if name == "path":
        return f"substr({c}, {len(prefix) + 1})"
    if data_type.lower() in FLOATS:
        return f"format_string('%.12g', {c})"
    return c


def _digest(cols: str) -> str:
    # As DECIMAL: a BIGINT sum of 64-bit hashes overflows under ANSI mode within a few rows.
    return f"coalesce(sum(CAST(xxhash64({cols}) AS DECIMAL(38, 0))), 0)"


def digest_sql(
    scope: Scope, schema: str, table: str, columns: list[tuple[str, str]], cutoff: date
) -> str:
    prefix = volume_prefix(scope)
    skip = EXCLUDED.get((schema, table), set())
    cols = [_column(n, t, prefix) for n, t in columns if n not in skip]
    where = COMPARED[(schema, table)]
    cond = f"WHERE `{where}` <= TIMESTAMP'{cutoff.isoformat()} 23:59:59'" if where else ""
    return (
        f"SELECT count(*) AS n, {_digest(', '.join(cols))} AS digest "
        f"FROM {scope.fq(schema, table)} {cond}"
    ).strip()


def outside_sql(scope: Scope, schema: str, table: str) -> str:
    """Rows whose `path` is not under the scope's own raw volume, compared case-sensitively."""
    prefix = volume_prefix(scope)
    return (
        "SELECT count(*) AS n, min(path) AS example FROM "
        f"{scope.fq(schema, table)} WHERE path IS NULL OR "
        f"substr(path, 1, {len(prefix)}) <> '{prefix}'"
    )


def table_digest(sql: Sql, scope: Scope, schema: str, table: str, cutoff: date) -> dict[str, Any]:
    cols = [
        (str(r["col_name"]), str(r["data_type"]))
        for r in sql(f"DESCRIBE TABLE {scope.fq(schema, table)}")
        if r.get("col_name") and not str(r["col_name"]).startswith("#")
    ]
    if any(n == "path" for n, _ in cols):
        bad = sql(outside_sql(scope, schema, table))[0]
        if int(bad["n"]):
            raise PathOutside(
                f"{schema}.{table}.path: {bad['n']} rows outside {volume_prefix(scope)}, "
                f"e.g. {bad['example']!r}"
            )
    row = sql(digest_sql(scope, schema, table, cols, cutoff))[0]
    return {"rows": int(row["n"]), "digest": str(row["digest"])}


def backtest_calls(sql: Sql, scope: Scope, cutoff: date) -> dict[str, Any]:
    t = scope.fq("signals", "signal_backtest")
    # The newest run only: a rebuilt scope holds one run where a migrated one keeps every earlier run.
    row = sql(
        f"SELECT count(*) AS n, {_digest('target_date, action')} AS digest "
        f"FROM {t} WHERE target_date <= DATE'{cutoff.isoformat()}' "
        f"AND backtest_run_id = (SELECT max_by(backtest_run_id, created_at) FROM {t})"
    )[0]
    return {"rows": int(row["n"]), "digest": str(row["digest"])}


def write(
    sql: Sql, scope: Scope, cutoff: date, champion: Mapping[str, str] | None
) -> dict[str, Any]:
    from weather_signal.signal import train

    return {
        "format": FORMAT,
        "cutoff": cutoff.isoformat(),
        "path_rule": f"strip /Volumes/<catalog>/<prefix>{RAW_SCHEMA}/; any other value fails",
        "tables": {f"{s}.{t}": table_digest(sql, scope, s, t, cutoff) for s, t in COMPARED},
        "champion": {k: (champion or {}).get(k) for k in train.COMPARED},
        "backtest": backtest_calls(sql, scope, cutoff),
    }


def compare(base: Mapping[str, Any], read: Callable[[date], Mapping[str, Any]]) -> list[str]:
    """Each difference as a line; empty means the scope equals the baseline."""
    if base.get("format") != FORMAT:
        return [f"baseline format {base.get('format')} is not {FORMAT}"]
    try:
        now = read(date.fromisoformat(base["cutoff"]))
    except PathOutside as err:
        return [str(err)]
    out = []
    for section in ("tables", "champion", "backtest"):
        a, b = base[section], now[section]
        keys = sorted(set(a) | set(b)) if section == "tables" else [section]
        for k in keys:
            want, have = (a.get(k), b.get(k)) if section == "tables" else (a, b)
            if want != have:
                out.append(f"{section} {k}: baseline {want}, now {have}")
    return out
