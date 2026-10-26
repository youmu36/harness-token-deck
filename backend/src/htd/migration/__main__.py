"""Inspect, snapshot, import, and verify legacy databases from the command line."""

import argparse
import sys
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from htd.adapters.sqlite.usage_codec import dumps
from htd.domain.common.errors import DomainError
from htd.domain.common.time import parse_instant
from htd.domain.usage.model import Harness
from htd.migration.convert import convert, prepare, require_empty_target
from htd.migration.legacy.reader import open_legacy, require_legacy_stopped
from htd.migration.native import is_native
from htd.migration.report import write_failure, write_report
from htd.migration.snapshot import snapshot
from htd.migration.verify import verify


def data_dir() -> Path:
    from htd.adapters.data_directory import resolve_data_directory

    return resolve_data_directory().root


def migration_dir() -> Path:
    return data_dir() / "migration" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def activate(staging: Path, target: Path) -> None:
    require_legacy_stopped()
    require_empty_target(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    # An open Windows database cannot be replaced; retain staging on failure.
    for suffix in ("-wal", "-shm"):
        if target.with_name(target.name + suffix).exists():
            raise DomainError("MIGRATION_TARGET_BUSY", "新版数据库仍有 WAL/SHM，请先退出新版")
    staging.replace(target)


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "inspect":
        if args.schema_only or is_native(args.source):
            with closing(open_legacy(args.source)) as db:
                tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                return {
                    "userVersion": db.execute("PRAGMA user_version").fetchone()[0],
                    "tables": tables,
                    "metadata": dict(db.execute("SELECT key,value FROM meta")) if "meta" in tables else {},
                    "schemaMetadata": [dict(row) for row in db.execute("SELECT * FROM schema_metadata")]
                    if "schema_metadata" in tables
                    else [],
                }
        prepared = prepare(args.source)
        differences: list[dict[str, Any]] = []
        audited = {(r["harness"], r["dedupe_key"]) for r in prepared.source.rows("usage_recalc_items")}
        for row in prepared.source.rows("usage_ledger"):
            resolved = prepared.catalog.resolve(Harness(row["harness"]), parse_instant(row["occurred_at"]))
            if (resolved.group.id if resolved.group else None) != row["price_group_id"]:
                billed = prepared.catalog.resolve(Harness(row["harness"]), parse_instant(row["billed_at"]))
                differences.append(
                    {
                        "harness": row["harness"],
                        "occurredAt": row["occurred_at"],
                        "billedAt": row["billed_at"],
                        "recordedGroup": row["price_group_id"],
                        "recordedVersion": row["price_version_id"],
                        "atBillingGroup": billed.group.id if billed.group else None,
                        "atBillingVersion": billed.version.id if billed.version else None,
                        "hasRecalculation": (row["harness"], row["dedupe_key"]) in audited,
                    }
                )
        return {
            "sourceVersion": prepared.source.identity.version,
            "userVersion": prepared.source.identity.user_version,
            "schema": prepared.source.identity.schema_name,
            "rowCounts": {table: len(rows) for table, rows in prepared.source.tables.items()},
            "groups": len(prepared.catalog.groups),
            "vendors": len(prepared.wallets),
            "settings": prepared.merged.main["settings"],
            "priceResolutionDifferences": sorted(differences, key=lambda r: r["hasRecalculation"])[:5],
        }
    if args.command == "snapshot":
        result = snapshot(args.source, (args.output_dir or migration_dir()) / "legacy-snapshot.sqlite3")
        return {"path": str(result.path), "sha256": result.sha256}
    if args.command == "verify":
        report_path = args.report or args.target.parent / "report.md"
        if report_path.resolve() in (args.source.resolve(), args.target.resolve()):
            raise DomainError("MIGRATION_OUTPUT_INVALID", "报告路径不能覆盖数据库")
        started = time.monotonic()
        try:
            result = verify(args.source, args.target)
            report = write_report(result, report_path, elapsed_seconds=time.monotonic() - started)
            return {"ok": result.ok, "report": str(report), "errors": result.errors}
        except Exception as error:
            write_failure(
                report_path, args.source, args.target, error, elapsed_seconds=time.monotonic() - started
            )
            raise
    directory = args.output_dir or migration_dir()
    if directory.exists() and any(directory.iterdir()):
        raise DomainError("MIGRATION_OUTPUT_INVALID", "导入输出目录必须为空，避免覆盖已有 staging 或报告")
    staging, report = directory / "staging.sqlite3", directory / "report.md"
    started = time.monotonic()
    try:
        target = args.target or data_dir() / "htd.sqlite3"
        prepared = convert(args.source, staging, target=target)
        result = verify(args.source, staging, conversion=prepared)
        write_report(result, report, elapsed_seconds=time.monotonic() - started)
        if result.ok and not args.dry_run:
            activate(staging, target)
        return {
            "ok": result.ok,
            "activated": result.ok and not args.dry_run,
            "target": str(target if result.ok and not args.dry_run else staging),
            "report": str(report),
            "errors": result.errors,
        }
    except Exception as error:
        write_failure(report, args.source, staging, error, elapsed_seconds=time.monotonic() - started)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="旧库只读迁移与校验")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "snapshot", "import", "verify"):
        command = commands.add_parser(name)
        command.add_argument("source", type=Path)
        if name == "inspect":
            command.add_argument("--schema-only", action="store_true")
        if name in ("snapshot", "import"):
            command.add_argument("--output-dir", type=Path)
        if name == "import":
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--target", type=Path)
        if name == "verify":
            command.add_argument("target", type=Path)
            command.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        result = run(args)
        sys.stdout.write(dumps(result) + "\n")
        return 0 if result.get("ok", True) else 1
    except Exception as error:
        sys.stderr.write(
            dumps(
                {
                    "ok": False,
                    "code": error.code if isinstance(error, DomainError) else "MIGRATION_FAILED",
                    "message": str(error),
                    "field": error.field if isinstance(error, DomainError) else None,
                }
            )
            + "\n"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
