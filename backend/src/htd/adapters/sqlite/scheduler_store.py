"""Persist scheduler controls and publish completed rounds after committing their diagnostics."""

from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.maintenance_repo import MaintenanceRepository
from htd.adapters.sqlite.sources_repo import SourcesRepository
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htd.application.ports import CollectionCompleted, ScanTrigger, SourceRecord
from htd.domain.usage.model import Harness


class SqliteSchedulerStore:
    def __init__(self, writer: WriteExecutor, readers: ReadPool) -> None:
        self.writer, self.readers = writer, readers

    def paused(self) -> bool:
        with self.readers.connection() as db:
            return MaintenanceRepository(db).state("collection_paused") == "true"

    def set_paused(self, paused: bool) -> None:
        def save(work: UnitOfWork) -> None:
            repo = MaintenanceRepository(work.connection)
            value = "true" if paused else "false"
            if repo.state("collection_paused") != value:
                repo.set_state(work, "collection_paused", value)
                work.touch("sources")

        self.writer.execute(save)

    def sources(self) -> tuple[SourceRecord, ...]:
        with self.readers.connection() as db:
            repo = SourcesRepository(db)
            return tuple(
                source for h in Harness if (source := repo.get(h)) is not None and source.scan_enabled
            )

    def watcher_status(self, available: bool) -> None:
        self.writer.execute(
            lambda work: SourcesRepository(work.connection).watcher_status(work, available=available)
        )

    def completed(self, trigger: ScanTrigger, *, watcher_available: bool) -> int:
        def complete(work: UnitOfWork) -> int:
            SourcesRepository(work.connection).watcher_status(work, available=watcher_available)
            revision = work.revision("sources")
            work.emit(CollectionCompleted(trigger, revision))
            return revision

        return self.writer.execute(complete)
