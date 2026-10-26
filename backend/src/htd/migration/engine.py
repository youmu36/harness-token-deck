"""Application-facing migration I/O reuses the CLI conversion and verification."""

import re
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from htd.adapters.sqlite.connection import open_connection
from htd.adapters.sqlite.operation_repo import OperationRepository
from htd.adapters.sqlite.usage_codec import dumps, object_json
from htd.adapters.sqlite.writer import UnitOfWork
from htd.application.ports import CommandStore, TaskContext
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Clock
from htd.migration.__main__ import activate
from htd.migration.convert import convert, require_empty_target
from htd.migration.legacy.reader import legacy_running, require_legacy_stopped
from htd.migration.report import write_failure, write_report
from htd.migration.snapshot import snapshot
from htd.migration.verify import verify


class Engine:
    def __init__(
        self,
        data_dir: Path,
        source: Path,
        store: CommandStore,
        clock: Clock,
        install: Callable[[Callable[[], None]], None],
    ) -> None:
        self.data_dir, self.source, self.store, self.clock, self.install = (
            data_dir,
            source,
            store,
            clock,
            install,
        )
        self.target = data_dir / "htd.sqlite3"

    def directory(self, migration_id: str) -> Path:
        if (
            re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", migration_id)
            is None
        ):
            raise DomainError("MIGRATION_NOT_FOUND", "迁移记录不存在，请重新演练")
        return self.data_dir / "migration" / migration_id

    def report_path(self, migration_id: str) -> str:
        return str(self.directory(migration_id) / "report.md")

    def source_path(self, source_path: str | None) -> Path:
        source = Path(source_path) if source_path is not None else self.source
        if not source.is_absolute() or "\x00" in str(source):
            raise DomainError("MIGRATION_SOURCE_PATH", "请选择旧版数据库的完整文件路径", "sourcePath")
        return source

    def detect(self, source_path: str | None = None) -> dict[str, object]:
        source = self.source_path(source_path)
        detected, running = source.is_file(), legacy_running()
        reason = None
        try:
            require_empty_target(self.target)
            empty = True
        except DomainError as error:
            empty, reason = False, error.message
        if not detected:
            reason = "未发现旧版数据库"
        elif running:
            reason = "请先退出旧版 Harness Token Usage 再迁移"
        return {
            "detected": detected,
            "sourcePath": str(source),
            "legacyRunning": running,
            "targetEmpty": empty,
            "canMigrate": detected and empty and not running,
            "reason": reason,
        }

    def run(
        self, migration_id: str, context: TaskContext, source_path: str | None = None
    ) -> dict[str, object]:
        source = self.source_path(source_path)
        directory = self.directory(migration_id)
        frozen, staging, report = (
            directory / "legacy-snapshot.sqlite3",
            directory / "staging.sqlite3",
            directory / "report.md",
        )
        started = time.monotonic()
        base: dict[str, object] = {
            "migrationId": migration_id,
            "sourcePath": str(source),
            "snapshotPath": str(frozen),
            "reportPath": str(report),
        }
        try:
            context.progress(0, 3)
            require_empty_target(self.target)
            snapshot(source, frozen, at=self.clock.now_utc())
            context.progress(1, 3)
            prepared = convert(frozen, staging, target=self.target)
            context.progress(2, 3)
            result = verify(frozen, staging, conversion=prepared)
            write_report(result, report, elapsed_seconds=time.monotonic() - started)
            summary = {
                **base,
                "ok": result.ok,
                "sourceVersion": result.source_version,
                "oldTotal": str(result.old_total),
                "newTotal": str(result.new_total),
                "counts": [
                    {"table": name, "old": old, "new": new} for name, (old, new) in result.counts.items()
                ],
                "vendors": [
                    {"name": wallet.name, "old": str(wallet.old), "new": str(wallet.new)}
                    for wallet in result.wallets
                ],
                "warnings": list(result.warnings),
                "errors": list(result.errors),
            }
            context.check_cancelled()
            with (directory / "result.json").open("x", encoding="utf-8") as stream:
                stream.write(dumps(summary))
            context.progress(3, 3)
            return summary
        except Exception as error:
            (directory / "result.json").unlink(missing_ok=True)
            write_failure(report, frozen, staging, error, elapsed_seconds=time.monotonic() - started)
            if isinstance(error, DomainError) and error.code == "TASK_CANCELLED":
                raise
            return {**base, "ok": False, "errors": [str(error)], "warnings": []}

    def activate(self, migration_id: str, operation_id: str) -> dict[str, object]:
        parameters: dict[str, object] = {"migrationId": migration_id}
        replay = self.store.execute(
            lambda work: self.store.replay(work, operation_id, "migration.activate", parameters)
        )
        if replay is not None:
            return replay
        require_legacy_stopped()
        directory = self.directory(migration_id)
        frozen, staging, report = (
            directory / "legacy-snapshot.sqlite3",
            directory / "staging.sqlite3",
            directory / "report.md",
        )
        if not (directory / "result.json").is_file():
            raise DomainError("MIGRATION_NOT_FOUND", "迁移报告不存在，请重新演练")
        summary = object_json((directory / "result.json").read_text(encoding="utf-8"))
        if summary.get("ok") is not True or not staging.is_file():
            raise DomainError("MIGRATION_VERIFICATION_FAILED", "迁移校验未通过，不能激活，请查看报告")
        checked = verify(frozen, staging)
        if not checked.ok:
            write_report(checked, directory / "activation-report.md")
            raise DomainError(
                "MIGRATION_VERIFICATION_FAILED", "staging 已改变，不能激活，请查看 activation-report.md"
            )
        result: dict[str, object] = {
            "activated": True,
            "sourcePath": summary.get("sourcePath", str(self.source)),
            "targetPath": str(self.target),
            "reportPath": str(report),
        }

        def replace() -> None:
            require_legacy_stopped()
            require_empty_target(self.target)
            # Collection now starts on first launch. Capture after the install
            # barrier has drained writers so no newly received request is lost.
            with (
                closing(open_connection(self.target, query_only=True)) as active,
                closing(open_connection(staging)) as db,
            ):
                active.execute("BEGIN")
                revisions = dict(active.execute("SELECT scope,value FROM revision"))
                cost_revision = active.execute(
                    "SELECT value FROM calibration_revision WHERE id=1"
                ).fetchone()[0]
                db.execute("BEGIN IMMEDIATE")
                try:
                    work = UnitOfWork(db)
                    db.executemany(
                        "INSERT OR IGNORE INTO operation VALUES (?,?,?,?)",
                        (tuple(row) for row in active.execute("SELECT * FROM operation")),
                    )
                    from htd.adapters.sqlite.key_codec import key_bytes

                    for row in active.execute("SELECT * FROM identity"):
                        db.execute(
                            "INSERT OR IGNORE INTO identity VALUES(?,?,?,?,?,?)",
                            (row[0], key_bytes(row[1]), *row[2:]),
                        )
                    retained = 0
                    for row in active.execute("SELECT * FROM inbox"):
                        key = row[0], key_bytes(row[1])
                        identity = db.execute(
                            "SELECT billed FROM identity WHERE harness=? AND dedupe_key=?", key
                        ).fetchone()
                        if identity is not None and not identity[0]:
                            retained += db.execute(
                                "INSERT OR IGNORE INTO inbox VALUES(?,?,?,?,?)", (*key, *row[2:])
                            ).rowcount
                            from htd.migration.pending import project

                            project(active, db, key[0], key[1], row[3])
                    previous = db.execute(
                        "SELECT value FROM meta WHERE key='legacy_pending_retained'"
                    ).fetchone()
                    db.execute(
                        "INSERT OR REPLACE INTO meta VALUES('legacy_pending_retained',?)",
                        (str(retained + (int(previous[0]) if previous else 0)),),
                    )
                    repo = OperationRepository(db)
                    if repo.replay(operation_id, "migration.activate", parameters) is None:
                        repo.record(
                            work,
                            operation_id,
                            "migration.activate",
                            parameters,
                            result,
                            at=self.clock.now_utc(),
                        )
                    for scope, revision in revisions.items():
                        db.execute(
                            "UPDATE revision SET value=MAX(value,?)+1 WHERE scope=?", (revision, scope)
                        )
                    db.execute(
                        "UPDATE calibration_revision SET value=MAX(value,?)+1 WHERE id=1", (cost_revision,)
                    )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.execute("PRAGMA journal_mode=DELETE")
            activate(staging, self.target)

        self.install(replace)
        return result
