"""History identity queries and atomic imports on the shared command writer."""

from collections.abc import Iterable, Mapping

from htd.adapters.sqlite.command_store import SqliteCommandStore, connection, read_snapshot
from htd.adapters.sqlite.history_repo import HistoryRepository
from htd.application.ports import SourcesChanged, UnitOfWork
from htd.domain.common.time import Instant
from htd.domain.usage.model import Harness, UsageEvent


class SqliteHistoryStore(SqliteCommandStore):
    def existing(self, events: Iterable[UsageEvent]) -> frozenset[tuple[Harness, str]]:
        with read_snapshot(self.readers) as db:
            return HistoryRepository(db).existing(events)

    def import_events(
        self, work: UnitOfWork, events: Iterable[UsageEvent], *, roots: Mapping[Harness, str], at: Instant
    ) -> dict[str, object]:
        result = HistoryRepository(connection(work)).import_events(work, events, roots=roots, at=at)
        if result.inserted:
            work.emit(SourcesChanged(work.revision("sources")))
        return {"inserted": result.inserted, "duplicates": result.duplicates, "tooOld": result.too_old}
