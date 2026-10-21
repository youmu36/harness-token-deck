"""Bounded source/day-aware query cache, retaining completed history apart from today."""

from collections import OrderedDict
from concurrent.futures import Future
from contextlib import closing
from threading import BoundedSemaphore, Lock
from typing import Literal

from htd.application.ports import StatisticsSnapshot, StatisticsStore
from htd.domain.common.time import Clock, Instant, format_instant, parse_instant
from htd.domain.statistics import Aggregator, Summary, Window, monitor_window, next_minute
from htd.domain.statistics.aggregator import ALL_SECTIONS, Section
from htd.domain.statistics.analysis import AnalysisQuery
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
        self.store, self.clock, self.capacity = store, clock, capacity
        self.max_bytes = max_bytes
        self._bytes = 0
        self._last_used = clock.monotonic()
        self._lock = Lock()
        self._slots = BoundedSemaphore(2)
        self._generation = 0
        self._pending: dict[tuple[int, CacheKey, Signature], Future[Summary]] = {}
        self._cache: OrderedDict[CacheKey, tuple[Signature, Aggregator, int]] = OrderedDict()

    def summary(
        self,
        harness: Harness | Literal["all"],
        window: Window,
        *,
        sections: frozenset[Section] | None = None,
        model_key: str | None = None,
    ) -> tuple[Summary, int]:
        selected = ALL_SECTIONS if sections is None else sections
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
            with self._lock:
                future = self._pending.get(request_key)
                owner = future is None
                if future is None:
                    future = Future[Summary]()
                    self._pending[request_key] = future
            revision = snapshot.revision
            if owner:
                try:
                    with self._slots:
                        result = self._build(
                            snapshot, harness, window, selected, model_key, parts, signatures, generation
                        )
                    future.set_result(result)
                except BaseException as error:
                    future.set_exception(error)
                    raise
                finally:
                    with self._lock:
                        self._pending.pop(request_key, None)
        # Followers release their read snapshot before waiting for the shared result.
        return future.result(), revision

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

    def long_term(self, start: Instant, end: Instant, harness: str) -> dict[str, object]:
        return self.store.long_term(start, end, harness)

    def acknowledge_quality(self, items: list[dict[str, str]]) -> dict[str, object]:
        return self.store.acknowledge_quality(items, self.clock.now_utc())

    def explore(self, query: dict[str, object]) -> dict[str, object]:
        with self._slots:
            return self.store.explore(query, self.clock.now_utc())

    def explore_options(self) -> dict[str, object]:
        with self._slots:
            return self.store.explore_options()

    def output_references(self, query: dict[str, object]) -> dict[str, object]:
        with self._slots:
            return self.store.output_references(query, self.clock.now_utc())

    def analytics(self, query: AnalysisQuery) -> dict[str, object]:
        with self._slots:
            return self.store.analytics(query, self.clock.now_utc())

    def auxiliary(self, start: Instant, end: Instant, harness: str) -> dict[str, object]:
        return self.store.auxiliary(start, end, harness)

    def cache_monitor(self, harness: Harness, mode: MonitorMode, offset_minutes: int) -> dict[str, object]:
        now = self.clock.now_utc()
        window = monitor_window(now, mode, offset_minutes)
        result, revision = self.summary(harness, window, sections=frozenset(("trend",)))
        monitor = self.store.monitor_data(harness.value, now)
        return {
            "start": format_instant(window.start),
            "end": format_instant(window.end),
            "offsetMinutes": offset_minutes,
            "grain": window.actual_grain,
            "windowKey": "|".join(map(str, window.key)),
            "nextRefreshAt": format_instant(next_minute(now)),
            **monitor,
            "trend": [point.to_dict() for point in result.trend],
            "revision": revision,
        }
