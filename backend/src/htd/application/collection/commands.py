"""Source configuration and collector controls using committed command receipts."""

from threading import Lock

from htd.application.ports import (
    CommandStore,
    ScanTrigger,
    SchedulerControl,
    SourceDiscovery,
    SourcesChanged,
    SourcesSnapshot,
    UnitOfWork,
)
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Clock, Instant, format_instant
from htd.domain.usage.model import Harness


def source_data(snapshot: SourcesSnapshot) -> dict[str, object]:
    counts = {count.harness: count for count in snapshot.counts}
    result: list[dict[str, object]] = []
    for source in snapshot.sources:
        count = counts.get(source.harness)
        result.append(
            {
                "harness": source.harness.value,
                "path": source.path,
                "startedAt": format_instant(source.started_at) if source.started_at else None,
                "lastScanAt": format_instant(source.last_scan_at) if source.last_scan_at else None,
                "status": source.status,
                "diagnostics": list(source.diagnostics),
                "unmeteredRequests": [item.to_dict() for item in source.unmetered_requests]
                if source.unmetered_requests is not None
                else None,
                "identityCount": count.identity_count if count else None,
                "processedCount": count.processed_count if count else None,
                "inboxCount": count.inbox_count if count else None,
                "ledgerCount": count.ledger_count if count else None,
            }
        )
    return {"sources": result, "revision": snapshot.revision, "paused": snapshot.paused}


class CollectionCommands:
    def __init__(
        self, store: CommandStore, discovery: SourceDiscovery, scheduler: SchedulerControl, clock: Clock
    ) -> None:
        self.store, self.discovery, self.scheduler, self.clock = store, discovery, scheduler, clock
        self._pause_lock = Lock()
        self._scan_lock = Lock()

    def list_sources(self) -> dict[str, object]:
        return source_data(self.store.sources())

    def validate_path(self, harness: Harness, path: str) -> dict[str, object]:
        available = self.discovery.available(harness, path)
        code = "source_database_unavailable" if harness == Harness.ZCODE else "source_directory_unavailable"
        return {
            "harness": harness.value,
            "path": path,
            "available": available,
            "diagnostics": [] if available else [code],
        }

    def configure(
        self,
        operation_id: str,
        expected_revision: int,
        *,
        harness: Harness | None = None,
        path: str | None = None,
    ) -> dict[str, object]:
        command = "sources.setPath" if harness is not None else "sources.autoDetect"
        parameters: dict[str, object] = {"expectedRevision": expected_revision}
        if harness is not None:
            parameters.update(harness=harness.value, path=path)

        def save(work: UnitOfWork) -> dict[str, object]:
            replay = self.store.replay(work, operation_id, command, parameters)
            if replay is not None:
                return replay
            snapshot = self.store.sources(work)
            if snapshot.revision != expected_revision:
                raise DomainError(
                    "SOURCES_REVISION_CONFLICT",
                    "来源状态已更新，请刷新后重试",
                    "expected_revision",
                    conflict_revision=snapshot.revision,
                )
            for current in Harness:
                if harness is not None and current != harness:
                    continue
                target = path if harness is not None else self.discovery.default_path(current)
                assert target is not None
                available = self.discovery.available(current, target)
                if harness is not None or available:
                    self.store.set_path(work, current, target, available=available)
            if work.revision("sources") != snapshot.revision:
                work.emit(SourcesChanged(work.revision("sources")))
            result = source_data(self.store.sources(work))
            self.store.record(work, operation_id, command, parameters, result, at=self.clock.now_utc())
            return result

        return self.store.execute(save)

    def status(self, *, details: bool = True) -> dict[str, object]:
        state = self.scheduler.status()
        data = source_data(self.store.sources(details=details))
        return {
            **data,
            "state": state.state,
            "nextPeriodicAt": None
            if state.state in ("paused", "stopping")
            else format_instant(state.next_periodic_at),
            "lastCompletedAt": format_instant(state.last_completed_at) if state.last_completed_at else None,
            "trigger": state.trigger,
            "lastError": state.last_error,
        }

    def set_paused(self, paused: bool, operation_id: str) -> dict[str, object]:
        parameters: dict[str, object] = {"paused": paused}
        command = "collection.setPaused"
        with self._pause_lock:
            replay = self.store.execute(
                lambda work: self.store.replay(work, operation_id, command, parameters)
            )
            if replay is not None:
                return replay
            result: dict[str, object] = {}

            def persist() -> None:
                def save(work: UnitOfWork) -> dict[str, object]:
                    # Another command can claim this ID between the initial read and this transaction.
                    replay = self.store.replay(work, operation_id, command, parameters)
                    if replay is not None:
                        return replay
                    before = work.revision("sources")
                    self.store.save_paused(work, paused)
                    revision = work.revision("sources")
                    if before != revision:
                        work.emit(SourcesChanged(revision))
                    saved: dict[str, object] = {"paused": paused, "revision": revision}
                    self.store.record(work, operation_id, command, parameters, saved, at=self.clock.now_utc())
                    return saved

                result.update(self.store.execute(save))

            self.scheduler.set_paused(paused, persist=persist)
            return result

    def scan_now(self, operation_id: str) -> dict[str, object]:
        command = "collection.scanNow"
        with self._scan_lock:
            replay = self.store.execute(lambda work: self.store.replay(work, operation_id, command, {}))
            if replay is not None:
                return replay
            completion = self.scheduler.scan_now().result()
            result: dict[str, object] = {
                "completedAt": format_instant(completion.completed_at),
                "nextPeriodicAt": format_instant(completion.next_periodic_at),
            }

            def record(work: UnitOfWork) -> dict[str, object]:
                replay = self.store.replay(work, operation_id, command, {})
                if replay is not None:
                    return replay
                self.store.record(work, operation_id, command, {}, result, at=self.clock.now_utc())
                return result

            return self.store.execute(record)

    def tick(
        self, completed_at: Instant, next_periodic_at: Instant, trigger: ScanTrigger
    ) -> dict[str, object]:
        return {
            "status": {
                source.harness.value: source.status for source in self.store.sources(details=False).sources
            },
            "nextPeriodicAt": format_instant(next_periodic_at),
            "lastCompletedAt": format_instant(completed_at),
            "trigger": trigger,
        }
