"""One scan round: discover, read, commit each file, then recover and bill the inbox."""

from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta

from htd.application.collection.billing import BillInbox, BillingResult
from htd.application.collection.cancellation import CollectionInterrupted, check_cancelled
from htd.application.ports import (
    CollectionStore,
    CommitBatchResult,
    ReadBatch,
    SourceDiscovery,
    SourceReader,
    SourceRecord,
    SourceStatus,
    TitleReader,
    TitleRequest,
    UnitOfWork,
    UnmeteredReader,
    UnmeteredRequest,
)
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Clock, Instant
from htd.domain.usage.model import Harness


@dataclass(frozen=True, slots=True)
class SourceScan:
    harness: Harness
    inserted: int
    duplicates: int
    files_read: int
    more: bool
    status: SourceStatus
    diagnostics: tuple[str, ...]
    unmetered_requests: tuple[UnmeteredRequest, ...] | None = None


@dataclass(frozen=True, slots=True)
class ScanResult:
    sources: tuple[SourceScan, ...]
    billing: BillingResult
    interrupted: bool = False

    @property
    def more(self) -> bool:
        return any(source.more for source in self.sources) or (
            self.billing.batch.more and not self.billing.blocked
        )


@dataclass(frozen=True, slots=True)
class _Cached:
    signature: tuple[int, int]
    diagnostics: tuple[str, ...]
    more: bool


class ScanRound:
    def __init__(
        self,
        store: CollectionStore,
        discovery: SourceDiscovery,
        readers: Mapping[Harness, SourceReader],
        billing: BillInbox,
        clock: Clock,
        *,
        unmetered: UnmeteredReader | None = None,
        titles: TitleReader | None = None,
        sources_initialized: bool = False,
    ) -> None:
        if set(readers) != set(Harness):
            raise ValueError("Readers must cover every Harness")
        self.store, self.discovery, self.readers = store, discovery, dict(readers)
        self.billing, self.clock = billing, clock
        self.unmetered = unmetered
        self.titles = titles
        self._cache: dict[tuple[Harness, str], _Cached] = {}
        self._initialized: set[Harness] = set(Harness) if sources_initialized else set()
        self._file_offsets: dict[Harness, int] = {}

    def run(self, *, should_stop: Callable[[], bool] = lambda: False) -> ScanResult:
        sources = self.collect(should_stop=should_stop)
        billing = self.billing.run(should_stop=should_stop)
        return ScanResult(sources, billing, should_stop())

    def collect(self, *, should_stop: Callable[[], bool] = lambda: False) -> tuple[SourceScan, ...]:
        results: list[SourceScan] = []
        for harness in Harness:
            if should_stop():
                break
            configured = self.store.source(harness)
            if configured is not None and not configured.scan_enabled:
                continue
            at = self.clock.now_utc()
            root: str | None = None
            try:
                source = self._begin(harness, at, should_stop)
                root = source.path
                results.append(self._collect_source(source, at, should_stop))
            except CollectionInterrupted:
                break
            except Exception as error:
                code = error.code if isinstance(error, DomainError) else "source_read_failed"
                diagnostics = (code,)
                if root is not None and code != "source_path_changed":
                    try:
                        self._finish(harness, root, at, "unavailable", diagnostics, should_stop)
                    except Exception as failed:
                        if isinstance(failed, DomainError) and failed.code == "source_path_changed":
                            diagnostics = (failed.code,)
                results.append(SourceScan(harness, 0, 0, 0, False, "unavailable", diagnostics))
        return tuple(results)

    def _begin(self, harness: Harness, at: Instant, should_stop: Callable[[], bool]) -> SourceRecord:
        def begin(work: UnitOfWork) -> SourceRecord:
            check_cancelled(should_stop)
            current = self.store.source(harness)
            default = self.discovery.default_path(harness)
            if current is None or (
                harness not in self._initialized
                and not self.discovery.available(harness, current.path)
                and self.discovery.available(harness, default)
            ):
                current = self.store.set_path(
                    work, harness, default, available=self.discovery.available(harness, default)
                )
            return self.store.begin_scan(work, harness, current.path, at=at)

        source = self.store.execute(begin)
        self._initialized.add(harness)
        return source

    def _finish(
        self,
        harness: Harness,
        root: str,
        at: Instant,
        status: SourceStatus,
        diagnostics: tuple[str, ...],
        should_stop: Callable[[], bool] = lambda: False,
        unmetered_requests: tuple[UnmeteredRequest, ...] | None = None,
        title_requests: tuple[TitleRequest, ...] = (),
    ) -> None:
        def finish(work: UnitOfWork) -> None:
            if should_stop():
                return
            self.store.finish_scan(
                work,
                harness,
                root,
                at=at,
                status=status,
                diagnostics=diagnostics,
                unmetered_requests=unmetered_requests,
                title_requests=title_requests,
            )

        self.store.execute(finish)

    def _collect_source(
        self, source: SourceRecord, at: Instant, should_stop: Callable[[], bool]
    ) -> SourceScan:
        harness, root = source.harness, source.path
        assert source.started_at is not None
        found = self.discovery.discover(harness, root)
        present = set(found.paths)
        self._cache = {
            key: value for key, value in self._cache.items() if key[0] != harness or key[1] in present
        }
        diagnostics = set(found.diagnostics)
        available, more = found.available, False
        inserted = duplicates = files_read = 0
        start_index = min(self._file_offsets.get(harness, 0), len(found.paths))
        self._file_offsets[harness] = 0
        attempts = 0
        reading_started = self.clock.monotonic()
        for index in range(start_index, len(found.paths)):
            path = found.paths[index]
            if attempts >= 64 or (index > start_index and self.clock.monotonic() - reading_started >= 0.25):
                self._file_offsets[harness] = index
                more = True
                break
            if should_stop():
                more = True
                break
            key = (harness, path)
            try:
                signature = self.discovery.signature(harness, path)
            except OSError:
                diagnostics.add("source_stat_failed")
                available = False
                self._cache.pop(key, None)
                continue
            cached = self._cache.get(key)
            if (
                signature is not None
                and cached is not None
                and cached.signature == signature
                and not cached.more
            ):
                diagnostics.update(cached.diagnostics)
                continue
            try:
                checkpoint = self.store.checkpoint(harness, path)
                attempts += 1
                batch = self.readers[harness].read(path, checkpoint, source.started_at)
                files_read += 1
                diagnostics.update(batch.diagnostics)
                available = available and batch.available
                more = more or batch.more
                if batch.retry:
                    self._cache.pop(key, None)
                    continue
                if should_stop():
                    more = True
                    break

                def commit(work: UnitOfWork, path: str = path, batch: ReadBatch = batch) -> CommitBatchResult:
                    check_cancelled(should_stop)
                    return self.store.commit_batch(
                        work,
                        harness,
                        root,
                        path,
                        batch.checkpoint,
                        batch.events,
                        at=at,
                        **({"compaction_keys": batch.compaction_keys} if batch.compaction_keys else {}),
                    )

                committed = self.store.execute(commit)
            except CollectionInterrupted:
                more = True
                break
            except Exception as error:
                if isinstance(error, DomainError) and error.code == "source_path_changed":
                    raise
                diagnostics.add("source_read_failed")
                available = False
                self._cache.pop(key, None)
                continue
            inserted += committed.inserted
            duplicates += committed.duplicates
            if signature is not None:
                if key not in self._cache and len(self._cache) >= 8192:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[key] = _Cached(signature, batch.diagnostics, batch.more)
        for (cached_harness, _), cached in self._cache.items():
            if cached_harness == harness:
                diagnostics.update(cached.diagnostics)
                more = more or cached.more
        status: SourceStatus = (
            "unavailable"
            if not available
            else ("baselining" if more else "degraded" if diagnostics else "watching")
        )
        codes = tuple(sorted(diagnostics))
        unmetered_requests = None
        if harness == Harness.CODEX and self.unmetered is not None and not should_stop():
            # Optional logs must never degrade collection or become a diagnostic code.
            with suppress(Exception):
                unmetered_requests = self.unmetered.read(root, Instant(at.value - timedelta(days=7)), at)
        title_requests: tuple[TitleRequest, ...] = ()
        if harness == Harness.CODEX and self.titles is not None and not should_stop():
            with suppress(Exception):
                start = max(source.started_at or at, Instant(at.value - timedelta(days=7)))
                title_requests = self.titles.read_titles(root, start, at) or ()
        if not should_stop():
            self._finish(harness, root, at, status, codes, should_stop, unmetered_requests, title_requests)
        return SourceScan(harness, inserted, duplicates, files_read, more, status, codes, unmetered_requests)
