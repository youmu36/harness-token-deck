"""Persist source state and checkpoints without resetting a source's initial collection boundary."""

import sqlite3
from typing import cast

from htd.adapters.sqlite.transaction import require_transaction
from htd.adapters.sqlite.unmetered_codec import decode_unmetered, encode_unmetered
from htd.adapters.sqlite.usage_codec import dumps, loads, object_json
from htd.application.ports import CheckpointRecord, SourceRecord, SourceStatus, UnitOfWork, UnmeteredRequest
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.usage.model import Harness

_STATUSES = {"pending", "unavailable", "baselining", "watching", "degraded"}


class SourcesRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get(self, harness: Harness) -> SourceRecord | None:
        harness = Harness(harness)
        row = self.connection.execute("SELECT * FROM source WHERE harness=?", (harness.value,)).fetchone()
        if row is None:
            return None
        diagnostics = loads(row["diagnostics_json"])
        if type(diagnostics) is not list or any(
            type(code) is not str for code in cast(list[object], diagnostics)
        ):
            raise DomainError("SOURCE_STATE_CORRUPT", "来源状态中的诊断数据损坏")
        return SourceRecord(
            harness,
            row["path"],
            parse_instant(row["started_at"]) if row["started_at"] else None,
            parse_instant(row["last_scan_at"]) if row["last_scan_at"] else None,
            cast(SourceStatus, row["status"]),
            tuple(cast(list[str], diagnostics)),
            decode_unmetered(row["unmetered_json"]),
            self.scan_enabled(harness),
        )

    def scan_enabled(self, harness: Harness) -> bool:
        row = self.connection.execute(
            "SELECT value FROM runtime_state WHERE key=?", ("scan_enabled." + harness.value,)
        ).fetchone()
        return row is None or row[0] != "false"

    def set_enabled(self, work: UnitOfWork, harness: Harness, enabled: bool, at: Instant) -> None:
        require_transaction(work, self.connection)
        if self.scan_enabled(harness) == enabled:
            return
        self.connection.execute(
            "INSERT INTO runtime_state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("scan_enabled." + harness.value, "true" if enabled else "false"),
        )
        from htd.adapters.sqlite.session_activity import source_observed
        from htd.domain.usage.session import SourceObservation

        source_observed(self.connection, SourceObservation(harness, at, "pending" if enabled else "disabled"))
        work.touch("sources")

    def ensure(self, work: UnitOfWork, harness: Harness, path: str, *, available: bool) -> SourceRecord:
        require_transaction(work, self.connection)
        existing = self.get(harness)
        return existing if existing is not None else self.set_path(work, harness, path, available=available)

    def set_path(self, work: UnitOfWork, harness: Harness, path: str, *, available: bool) -> SourceRecord:
        require_transaction(work, self.connection)
        harness = Harness(harness)
        if type(path) is not str or type(available) is not bool:
            raise ValueError("Invalid source path or availability")
        old = self.get(harness)
        if old is not None and old.path == path:
            return old
        status = "pending" if available else "unavailable"
        code = "source_database_unavailable" if harness == Harness.ZCODE else "source_directory_unavailable"
        diagnostics = dumps([] if available else [code])
        self.connection.execute(
            "INSERT INTO source(harness,path,started_at,last_scan_at,status,diagnostics_json) "
            "VALUES (?,?,NULL,NULL,?,?) "
            "ON CONFLICT(harness) DO UPDATE SET path=excluded.path,status=excluded.status,"
            "diagnostics_json=excluded.diagnostics_json,unmetered_json=NULL",
            (harness.value, path, status, diagnostics),
        )
        work.touch("sources")
        result = self.get(harness)
        assert result is not None
        return result

    def require_path(self, harness: Harness, root: str) -> SourceRecord:
        source = self.get(harness)
        if source is None:
            raise DomainError("SOURCE_NOT_FOUND", "来源尚未配置", "harness")
        if source.path != root:
            raise DomainError("source_path_changed", "来源路径已变更，等待重新连接", "path")
        return source

    def begin_scan(self, work: UnitOfWork, harness: Harness, root: str, *, at: Instant) -> SourceRecord:
        require_transaction(work, self.connection)
        source = self.require_path(harness, root)
        if not source.scan_enabled:
            raise DomainError("SOURCE_SCAN_DISABLED", "此来源扫描已停用", "harness")
        from htd.adapters.sqlite.session_activity import source_observed
        from htd.domain.usage.session import SourceObservation

        source_observed(self.connection, SourceObservation(harness, at, "reading"))
        if source.started_at is None:
            self.connection.execute(
                "UPDATE source SET started_at=COALESCE(started_at,?) WHERE harness=?",
                (format_instant(at), source.harness.value),
            )
            work.touch("sources")
            source = self.require_path(harness, root)
        return source

    def finish_scan(
        self,
        work: UnitOfWork,
        harness: Harness,
        root: str,
        *,
        at: Instant,
        status: SourceStatus,
        diagnostics: tuple[str, ...],
        unmetered_requests: tuple[UnmeteredRequest, ...] | None = None,
    ) -> SourceRecord:
        require_transaction(work, self.connection)
        from htd.adapters.sqlite.session_activity import source_observed
        from htd.domain.usage.session import SourceObservation

        source_observed(
            self.connection,
            SourceObservation(
                harness,
                at,
                "ready"
                if status == "watching"
                else "pending"
                if status in {"pending", "baselining"}
                else "failed",
            ),
        )
        source = self.require_path(harness, root)
        if status not in _STATUSES or any(type(code) is not str or not code for code in diagnostics):
            raise ValueError("Invalid source status or diagnostics")
        normalized = tuple(sorted(set(diagnostics)))
        if (source.last_scan_at, source.status, source.diagnostics, source.unmetered_requests) != (
            at,
            status,
            normalized,
            unmetered_requests,
        ):
            self.connection.execute(
                "UPDATE source SET last_scan_at=?,status=?,diagnostics_json=?,unmetered_json=? "
                "WHERE harness=?",
                (
                    format_instant(at),
                    status,
                    dumps(normalized),
                    encode_unmetered(unmetered_requests),
                    source.harness.value,
                ),
            )
            work.touch("sources")
        return self.require_path(harness, root)

    def checkpoint(self, harness: Harness, source_key: str) -> CheckpointRecord:
        row = self.connection.execute(
            "SELECT state_json FROM checkpoint WHERE harness=? AND source_key=?",
            (Harness(harness).value, source_key),
        ).fetchone()
        if row is None:
            return CheckpointRecord()
        try:
            return CheckpointRecord(object_json(row[0]))
        except (ValueError, RecursionError):
            return CheckpointRecord(diagnostics=("invalid_checkpoint",))

    def watcher_status(self, work: UnitOfWork, *, available: bool) -> None:
        require_transaction(work, self.connection)
        code = "watcher_unavailable_polling"
        for harness in Harness:
            source = self.get(harness)
            if source is None:
                continue
            if not source.scan_enabled:
                continue
            diagnostics = set(source.diagnostics)
            diagnostics.discard(code)
            if not available:
                diagnostics.add(code)
            normalized = tuple(sorted(diagnostics))
            status = source.status
            if status == "watching" and normalized:
                status = "degraded"
            elif status == "degraded" and not normalized:
                status = "watching"
            if (normalized, status) != (source.diagnostics, source.status):
                self.connection.execute(
                    "UPDATE source SET diagnostics_json=?,status=? WHERE harness=?",
                    (dumps(list(normalized)), status, harness.value),
                )
                work.touch("sources")

    def save_checkpoint(
        self, work: UnitOfWork, harness: Harness, source_key: str, state: dict[str, object], *, at: Instant
    ) -> None:
        require_transaction(work, self.connection)
        if type(source_key) is not str or not source_key or type(state) is not dict:
            raise ValueError("Checkpoint requires a source key and JSON object state")
        self.connection.execute(
            "INSERT INTO checkpoint VALUES (?,?,?,?) "
            "ON CONFLICT(harness,source_key) DO UPDATE SET state_json=excluded.state_json,"
            "updated_at=excluded.updated_at",
            (Harness(harness).value, source_key, dumps(state), format_instant(at)),
        )
        work.touch("sources")
