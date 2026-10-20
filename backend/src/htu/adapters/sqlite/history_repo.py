"""Import already-scanned historical events atomically without changing source boundaries."""

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta

from htu.adapters.sqlite.key_codec import key_bytes
from htu.adapters.sqlite.sources_repo import SourcesRepository
from htu.adapters.sqlite.transaction import require_transaction
from htu.adapters.sqlite.usage_codec import decode_event, encode_event
from htu.application.ports import UnitOfWork
from htu.domain.common.errors import DomainError
from htu.domain.common.time import Instant, format_instant, parse_instant
from htu.domain.usage.model import Harness, UsageEvent


@dataclass(frozen=True, slots=True)
class HistoryImportResult:
    inserted: int
    duplicates: int
    too_old: int


class HistoryRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def existing(self, events: Iterable[UsageEvent]) -> frozenset[tuple[Harness, str]]:
        return frozenset(
            (event.harness, event.dedupe_key)
            for event in events
            if self.connection.execute(
                "SELECT 1 FROM identity WHERE harness=? AND dedupe_key=?",
                (event.harness.value, key_bytes(event.dedupe_key)),
            ).fetchone()
        )

    def import_events(
        self, work: UnitOfWork, events: Iterable[UsageEvent], *, roots: Mapping[Harness, str], at: Instant
    ) -> HistoryImportResult:
        require_transaction(work, self.connection)
        sources = SourcesRepository(self.connection)
        for harness, root in roots.items():
            current = sources.get(harness)
            if current is None or current.path != root:
                raise DomainError("SOURCE_PATH_CHANGED", "来源路径已变化，请重新扫描历史", "path")
        inserted = duplicates = too_old = 0
        cutoff = Instant(at.value - timedelta(days=14))
        for count, scanned in enumerate(events, 1):
            if count > 500_000:
                raise DomainError("HISTORY_SCAN_TOO_LARGE", "历史记录超过 50 万条")
            if type(scanned) is not UsageEvent or scanned.harness not in roots:
                raise ValueError("Historical event has no scanned source path")
            event = decode_event(encode_event(scanned))
            if parse_instant(event.occurred_at) < cutoff:
                too_old += 1
                continue
            cursor = self.connection.execute(
                "INSERT INTO identity VALUES (?,?,?,0,?,'history') "
                "ON CONFLICT(harness,dedupe_key) DO NOTHING",
                (event.harness.value, key_bytes(event.dedupe_key), event.occurred_at, format_instant(at)),
            )
            if not cursor.rowcount:
                duplicates += 1
                continue
            self.connection.execute(
                "INSERT INTO inbox VALUES (?,?,?,?,?)",
                (
                    event.harness.value,
                    key_bytes(event.dedupe_key),
                    event.occurred_at,
                    encode_event(event),
                    format_instant(at),
                ),
            )
            inserted += 1
        self.connection.execute(
            "INSERT INTO diagnostic_log VALUES(NULL,?,NULL,'history_import',?)",
            (format_instant(at), f"新增 {inserted} 条，重复 {duplicates} 条，过期 {too_old} 条"),
        )
        if inserted:
            work.touch("sources")
        return HistoryImportResult(inserted, duplicates, too_old)
