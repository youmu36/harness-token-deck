"""Bounded source/day-aware query cache, retaining completed history apart from today."""

from collections import OrderedDict
from collections.abc import Generator
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import closing, contextmanager
from threading import Lock
from typing import Literal

from htd.application.ports import EffectiveCostSnapshot, StatisticsSnapshot, StatisticsStore
from htd.application.ports.query_control import QuerySlots, SharedReadControl, current_read
from htd.application.query_control import analysis_read, check_read
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Clock, Instant, parse_instant
from htd.domain.statistics import Aggregator, Summary, Window, monitor_window
from htd.domain.statistics.aggregator import ALL_SECTIONS, Section
from htd.domain.statistics.analysis import AnalysisQuery
from htd.domain.statistics.query import QueryPlan
from htd.domain.statistics.window import Grain, MonitorMode
from htd.domain.usage.model import Harness

type CacheKey = tuple[
    Harness | Literal["all"],
    tuple[str, str, int, Grain],
    frozenset[Section],
    str | None,
]
type Signature = tuple[tuple[str, str, int], ...]


class Statistics:
    def __init__(
        self, store: StatisticsStore, clock: Clock, *, capacity: int = 8, max_bytes: int = 4 * 1024 * 1024
    ) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("Statistics cache capacity must be positive")
        self.store, self.clock, self.capacity = store, clock, min(capacity, 6)
        # Reserve two entries / one MiB for the same service's monitor snapshots.
        self.max_bytes = min(max_bytes, 3 * 1024**2)
        self._bytes = 0
        self._last_used = clock.monotonic()
        self._lock = Lock()
        self._slots: QuerySlots = getattr(store, "query_slots", QuerySlots())
        self._generation = 0
        self._pending: dict[tuple[int, CacheKey, Signature], Future[Summary]] = {}
        self._pending_controls: dict[tuple[int, CacheKey, Signature], SharedReadControl] = {}
        self._cache: OrderedDict[CacheKey, tuple[Signature, Aggregator, int]] = OrderedDict()

    def summary(
        self,
        harness: Harness | Literal["all"],
        window: Window,
        *,
        sections: frozenset[Section] | None = None,
        model_key: str | None = None,
    ) -> tuple[Summary, int]:
        with analysis_read(None):
            for _ in range(3):
                try:
                    return self._summary(harness, window, sections=sections, model_key=model_key)
                except DomainError as error:
                    if error.code != "READ_REPLAN":
                        raise
                    check_read()
            raise DomainError("READ_CHANGED", "用量持续变化，请按当前条件重试", retryable=True)

    def _summary(
        self,
        harness: Harness | Literal["all"],
        window: Window,
        *,
        sections: frozenset[Section] | None = None,
        model_key: str | None = None,
    ) -> tuple[Summary, int]:
        selected = ALL_SECTIONS if sections is None else sections
        costs = None
        with self.store.snapshot() as snapshot:
            with self._lock:
                if self.clock.monotonic() - self._last_used >= 60:
                    self._clear()
                self._last_used = self.clock.monotonic()
                generation = self._generation
            # The immutable prefix survives new usage in today's tail. One snapshot
            # covers both parts, so late arrivals and re-pricing cannot mix revisions.
            boundary = Instant(window.end.value.replace(hour=0, minute=0, second=0, microsecond=0))
            bounds = (
                [(window.start, boundary), (boundary, window.end)]
                if window.start < boundary < window.end
                and (window.end.value - window.start.value).total_seconds() > 72 * 3600
                and not snapshot.has_daily_history(window.start, window.end, harness)
                else [(window.start, window.end)]
            )
            parts = [Window(start, end, window.offset_minutes, window.actual_grain) for start, end in bounds]
            signatures = [snapshot.signature(part.start, part.end, harness) for part in parts]
            request_key: tuple[int, CacheKey, Signature] = (
                generation,
                (harness, window.key, selected, model_key),
                tuple(item for signature in signatures for item in signature),
            )
            revision = snapshot.revision
            if isinstance(snapshot, EffectiveCostSnapshot):
                costs = snapshot.costs(window.start, window.end, harness, model_key)
        with self._lock:
            future = self._pending.get(request_key)
            owner = future is None
            if future is None:
                future = Future[Summary]()
                self._pending[request_key] = future
                self._pending_controls[request_key] = SharedReadControl()
            shared = self._pending_controls[request_key]
            reader = current_read.get()
            assert reader is not None
            shared.join(reader)
        if owner:
            token = current_read.set(shared)
            try:
                with self._read_slot(), self.store.snapshot() as snapshot:
                    current = [snapshot.signature(part.start, part.end, harness) for part in parts]
                    if current != signatures:
                        raise DomainError("READ_REPLAN", "等待期间读取范围已变化", retryable=True)
                    result = self._build(
                        snapshot, harness, window, selected, model_key, parts, signatures, generation
                    )
                future.set_result(result)
            except BaseException as error:
                future.set_exception(error)
                raise
            finally:
                current_read.reset(token)
                with self._lock:
                    self._pending.pop(request_key, None)
                    self._pending_controls.pop(request_key, None)
                if future.exception() is not None:
                    shared.leave(reader)
        # Followers release their read snapshot before waiting for the shared result.
        try:
            while True:
                check_read()
                try:
                    result = future.result(timeout=0.05)
                    return costs.summary(result) if costs is not None else result, revision
                except FutureTimeout:
                    continue
        finally:
            shared.leave(reader)

    def _build(
        self,
        snapshot: StatisticsSnapshot,
        harness: Harness | Literal["all"],
        window: Window,
        selected: frozenset[Section],
        model_key: str | None,
        parts: list[Window],
        signatures: list[Signature],
        generation: int,
    ) -> Summary:
        result = Aggregator(
            window.offset_minutes,
            window.actual_grain,
            window.start,
            window.end,
            harness=harness,
            sections=selected,
            model_key=model_key,
        )
        for part, signature in zip(parts, signatures, strict=True):
            start, end = part.start, part.end
            key: CacheKey = harness, part.key, selected, model_key
            with self._lock:
                cached = self._cache.get(key)
                if cached is not None and cached[0] == signature:
                    self._cache.move_to_end(key)
                extensions = [
                    (cached_key[1][1], item[1])
                    for cached_key, item in self._cache.items()
                    if cached_key[0] == harness
                    and cached_key[1][0] == part.key[0]
                    and cached_key[1][2:] == part.key[2:]
                    and cached_key[2:] == key[2:]
                    and cached_key[1][1] < part.key[1]
                    and item[0] == signature
                    and not item[1].has_daily_history
                ]
                extension = max(extensions, key=lambda item: item[0]) if extensions else None
            if cached is not None and cached[0] == signature:
                aggregate = cached[1]
            else:
                aggregate = Aggregator(
                    part.offset_minutes,
                    part.actual_grain,
                    start,
                    end,
                    harness=harness,
                    sections=selected,
                    model_key=model_key,
                )
                read_start = start
                if extension is not None:
                    read_start = parse_instant(extension[0])
                    aggregate.merge(extension[1])
                source = (
                    snapshot.rows(read_start, end, harness, model_key=model_key, coarse=True)
                    if part.actual_grain != "minute" and part.offset_minutes % 60 == 0
                    else snapshot.rows(read_start, end, harness)
                    if model_key is None
                    else snapshot.rows(read_start, end, harness, model_key=model_key)
                )
                with closing(source) as rows:
                    for row in rows:
                        check_read()
                        aggregate.push(row)
                size = aggregate.estimated_bytes() + len(signature) * 200
                with self._lock:
                    if generation == self._generation and size <= self.max_bytes:
                        previous = self._cache.pop(key, None)
                        if previous is not None:
                            self._bytes -= previous[2]
                        self._cache[key] = signature, aggregate, size
                        self._bytes += size
                        while len(self._cache) > self.capacity or self._bytes > self.max_bytes:
                            _, removed = self._cache.popitem(last=False)
                            self._bytes -= removed[2]
            result.merge(aggregate)
        return result.finish()

    def _clear(self) -> None:
        self._generation += 1
        self._cache.clear()
        self._bytes = 0

    def diagnostics(self) -> dict[str, int]:
        with self._lock:
            return {"entries": len(self._cache), "estimatedBytes": self._bytes, "maxBytes": self.max_bytes}

    def invalidate(self) -> None:
        with self._lock:
            self._clear()
        clear = getattr(self.store, "clear_analysis_cache", None)
        if clear:
            clear()

    def expire_idle_cache(self) -> None:
        with self._lock:
            if self._cache and self.clock.monotonic() - self._last_used >= 60:
                self._clear()
        expire = getattr(self.store, "expire_idle_cache", None)
        if expire:
            expire()

    def long_term(self, start: Instant, end: Instant, harness: str) -> dict[str, object]:
        with analysis_read(None), self._read_slot():
            return self.store.long_term(start, end, harness)

    def evidence(self, query: dict[str, object]) -> dict[str, object]:
        identity = query.get("queryId")
        with analysis_read(identity if isinstance(identity, str) else None), self._read_slot():
            return self.store.evidence(query, self.clock.now_utc())

    def acknowledge_quality(self, items: list[dict[str, str]]) -> dict[str, object]:
        return self.store.acknowledge_quality(items, self.clock.now_utc())

    def explore(self, query: dict[str, object]) -> dict[str, object]:
        identity = query.get("queryId")
        with analysis_read(identity if isinstance(identity, str) else None):
            now = self.clock.now_utc()
            return self.store.explore(QueryPlan.build(query, now).parameters, now)

    @contextmanager
    def _read_slot(self) -> Generator[None, None, None]:
        with self._slots.acquire():
            yield

    def explore_options(self, query: dict[str, object] | None = None) -> dict[str, object]:
        with self._read_slot():
            return self.store.explore_options(query)

    def output_references(self, query: dict[str, object]) -> dict[str, object]:
        identity = query.get("queryId")
        with analysis_read(identity if isinstance(identity, str) else None), self._read_slot():
            return self.store.output_references(query, self.clock.now_utc())

    def analytics(self, query: AnalysisQuery) -> dict[str, object]:
        with self._read_slot():
            return self.store.analytics(query, self.clock.now_utc())

    def auxiliary(self, start: Instant, end: Instant, harness: str) -> dict[str, object]:
        return self.store.auxiliary(start, end, harness)

    def cache_monitor(
        self,
        harness: Harness,
        mode: MonitorMode,
        offset_minutes: int,
        session_ref: str | None = None,
        cursor: str | None = None,
        query_id: str | None = None,
        chart_session_ref: str | None = None,
    ) -> dict[str, object]:
        monitor_window(self.clock.now_utc(), mode, offset_minutes)
        with analysis_read(query_id):
            return self.store.monitor_data(
                harness.value, self.clock.now_utc(), session_ref, offset_minutes, cursor, chart_session_ref
            )
