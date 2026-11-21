"""`weather-signal`: the owner's commands on the Mac, over Databricks Connect serverless (never a warehouse)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from weather_signal import config
from weather_signal.catalog import setup
from weather_signal.catalog.ddl import TableSpec, specs
from weather_signal.deploy import archive_export
from weather_signal.deploy import command as bootstrap_command
from weather_signal.guard import repair
from weather_signal.guard.catalog import Catalog


def say(text: str) -> None:
    sys.stdout.write(text + "\n")


def _names(a: argparse.Namespace) -> config.Names:
    return config.Names(a.catalog, a.schema_prefix)


def _catalog(a: argparse.Namespace) -> Catalog:
    from weather_signal.guard.spark_catalog import SparkCatalog
    from weather_signal.spark import spark_session

    return SparkCatalog(spark_session(a.profile), _names(a))


def _spec(a: argparse.Namespace) -> TableSpec:
    n = _names(a)
    by_short = {name.rsplit(".", 1)[1]: s for name, s in specs(n).items()}
    if a.table not in by_short:
        raise SystemExit(f"--table must be one of {sorted(by_short)}")
    return by_short[a.table]


def cmd_setup(a: argparse.Namespace) -> int:
    cat, n = _catalog(a), _names(a)
    ids = setup.run(cat, n)
    for sql in setup.grants(n, a.runner, a.agent):
        cat.execute(sql)
    say(json.dumps(ids, indent=2))
    return 0


def cmd_ui_export(a: argparse.Namespace) -> int:
    from weather_signal.serving import export

    try:
        if a.from_file:
            doc = json.loads(Path(a.from_file).read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                raise export.ExportError(f"{a.from_file} is not a snapshot object")
        else:
            doc = export.download(
                f"{_names(a).volume_path('signals', 'ui')}/snapshot.json", a.profile
            )
        now = datetime.now(UTC)
        say(str(export.write(doc, Path(a.out), force=a.force, now=now, stale_ok=a.stale_ok)))
    except (export.ExportError, OSError, ValueError) as err:
        say(f"not exported: {err}")
        return 1
    return 0


def cmd_guard_status(a: argparse.Namespace) -> int:
    cat = _catalog(a)
    for r in cat.guard_rows():
        pending = [x.act_id for x in cat.acts(r.table_name) if x.status != "complete"]
        say(
            f"{r.table_name}\tid {r.table_id}\tchecked {r.checked_version}\t"
            f"open {r.open_incident}\tpending {pending or '-'}"
        )
    return 0


def cmd_guard_repair(a: argparse.Namespace) -> int:
    say(
        repair.repair(
            _catalog(a), _spec(a), datetime.now(UTC), to_version=a.to_version, rebuild=a.rebuild
        )
    )
    return 0


def cmd_guard_ack(a: argparse.Namespace) -> int:
    say(repair.ack(_catalog(a), _spec(a), a.through))
    return 0


def cmd_guard_adopt_id(a: argparse.Namespace) -> int:
    say(repair.adopt_id(_catalog(a), _spec(a)))
    return 0


def _guard_parser(p: argparse.ArgumentParser) -> None:
    g = p.add_subparsers(dest="guard_cmd", required=True)
    g.add_parser("status").set_defaults(fn=cmd_guard_status)
    r = g.add_parser("repair")
    r.add_argument("--table", required=True)
    r.add_argument("--to-version", type=int)
    r.add_argument("--rebuild", action="store_true")
    r.set_defaults(fn=cmd_guard_repair)
    k = g.add_parser("ack")
    k.add_argument("--table", required=True)
    k.add_argument("--through", type=int, required=True)
    k.set_defaults(fn=cmd_guard_ack)
    d = g.add_parser("adopt-id")
    d.add_argument("--table", required=True)
    d.set_defaults(fn=cmd_guard_adopt_id)


def _ui_export_parser(e: argparse.ArgumentParser) -> None:
    e.add_argument("--out", required=True, help="folder to write index.html into")
    e.add_argument("--from", dest="from_file", help="a local snapshot.json instead of the volume")
    e.add_argument(
        "--force", action="store_true", help="write into a non-empty folder the export did not make"
    )
    e.add_argument(
        "--stale-ok", action="store_true", help="export a snapshot older than the 75 min bound"
    )
    e.set_defaults(fn=cmd_ui_export)


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="weather-signal")
    ap.add_argument("--catalog", default="workspace")
    ap.add_argument("--schema-prefix", default="", help="dev_ for the dev target")
    ap.add_argument("--profile", help="unset: the SDK reads DATABRICKS_CONFIG_PROFILE")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="create or reconcile tables, views, functions and grants")
    s.add_argument("--runner", help="the runner principal's application id")
    s.add_argument("--agent", help="the agent principal's application id; omit for the fallback")
    s.set_defaults(fn=cmd_setup)
    _guard_parser(sub.add_parser("guard"))
    bootstrap_command.add_parser(sub)
    archive_export.add_parser(sub)
    _ui_export_parser(
        sub.add_parser("ui-export", help="the pages and the latest snapshot, for file://")
    )
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    a = parser().parse_args(argv)
    return int(a.fn(a))
