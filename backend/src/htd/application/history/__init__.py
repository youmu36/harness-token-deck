"""Isolated historical scans, bounded result cache and atomic selected-date imports."""

from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import timedelta, timezone
from threading import Lock
from typing import cast
from uuid import uuid4

from htd.application.collection.billing import BillInbox
from htd.application.command import execute
from htd.application.ports import (
    CheckpointRecord,
    SourceDiscovery,
    SourceReader,
    TaskContext,
    TaskControl,
    UnitOfWork,
)
from htd.application.ports.maintenance import HistoryStore
from htd.application.tasks import start_task
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Clock, Instant, format_instant, parse_instant
from htd.domain.usage.model import Harness, UsageEvent

MAX_HISTORY_EVENTS = 500_000


@dataclass(frozen=True, slots=True)
class ScanResult:
    events: tuple[UsageEvent, ...]
    roots: dict[Harness, str]
    dates: dict[str, tuple[UsageEvent, ...]]
    finished: float


class History:
    def __init__(
        self,
        store: HistoryStore,
        discovery: SourceDiscovery,
        readers: dict[Harness, SourceReader],
        tasks: TaskControl,
        billing: BillInbox,
        clock: Clock,
    ) -> None:
        self.store, self.discovery, self.readers, self.tasks, self.billing, self.clock = (
            store,
            discovery,
            readers,
            tasks,
            billing,
            clock,
        )
        self._lock = Lock()
        self._results: OrderedDict[str, ScanResult] = OrderedDict()

    def scan(self, parameters: dict[str, object], operation_id: str) -> dict[str, object]:
        return start_task(
            self.store,
            self.clock,
            self.tasks,
            "history.scan",
            parameters,
            operation_id,
            lambda context: self.run(parameters, context),
        )

    def run(self, parameters: dict[str, object], context: TaskContext) -> dict[str, object]:
        end = self.clock.now_utc()
        offset = timedelta(minutes=cast(int, parameters["offsetMinutes"]))
        local = end.value.astimezone(timezone(offset))
        start = Instant(
            local.replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=cast(int, parameters["days"]) - 1)
        )
        roots: dict[Harness, str] = {}
        files: list[tuple[Harness, str]] = []
        diagnostics: set[str] = set()
        for source in self.store.sources().sources:
            context.check_cancelled()
            if parameters["harness"] not in ("all", source.harness.value):
                continue
            roots[source.harness] = source.path
            discovered = self.discovery.discover(source.harness, source.path)
            files.extend((source.harness, path) for path in discovered.paths)
            diagnostics.update(discovered.diagnostics)
        found: dict[tuple[Harness, str], UsageEvent] = {}
        compacted: set[str] = set()
        for index, (harness, path) in enumerate(files):
            context.progress(index, len(files))
            checkpoint = CheckpointRecord()
            while True:
                context.check_cancelled()
                batch = self.readers[harness].read(path, checkpoint, start)
                diagnostics.update(batch.diagnostics)
                compacted.update(batch.compaction_keys)
                for event in batch.events:
                    if not start <= parse_instant(event.occurred_at) <= end:
                        continue
                    key = (event.harness, event.dedupe_key)
                    if key in found and found[key] != event:
                        diagnostics.add("history_identity_conflict")
                    found.setdefault(key, event)
                    if len(found) > MAX_HISTORY_EVENTS:
                        raise DomainError("HISTORY_SCAN_TOO_LARGE", "历史记录超过 50 万条")
                if not batch.more or batch.retry or not batch.available:
                    break
                state = dict(batch.checkpoint)
                if harness == Harness.ZCODE:
                    state["pending"] = []
                if state == checkpoint.state:
                    diagnostics.add("source_scan_stalled")
                    break
                checkpoint = CheckpointRecord(state, batch.diagnostics)
        from dataclasses import replace

        events = tuple(
            replace(event, issues=(*event.issues, "context_compacted"))
            if event.harness == Harness.CODEX
            and event.dedupe_key in compacted
            and "context_compacted" not in event.issues
            else event
            for event in found.values()
        )
        existing: set[tuple[Harness, str]] = set()
        for index in range(0, len(events), 500):
            context.check_cancelled()
            existing.update(self.store.existing(events[index : index + 500]))
        dates: dict[str, list[UsageEvent]] = {}
        for event in events:
            day = (parse_instant(event.occurred_at).value + offset).date().isoformat()
            dates.setdefault(day, []).append(event)
        new_counts = Counter(
            (parse_instant(e.occurred_at).value + offset).date().isoformat()
            for e in events
            if (e.harness, e.dedupe_key) not in existing
        )
        context.progress(len(files), len(files))
        scan_id = str(uuid4())
        with self._lock:
            self._prune()
            context.check_cancelled()
            self._results[scan_id] = ScanResult(
                events, roots, {day: tuple(rows) for day, rows in dates.items()}, self.clock.monotonic()
            )
            while len(self._results) > 4:
                self._results.popitem(last=False)
        return {
            "scanId": scan_id,
            "start": format_instant(start),
            "end": format_instant(end),
            "dates": [
                {"date": day, "recordCount": len(dates[day]), "newCount": new_counts[day]}
                for day in sorted(dates, reverse=True)
            ],
            "models": sorted({e.model_id for e in events if e.model_id is not None}),
            "diagnostics": sorted(diagnostics),
        }

    def _prune(self) -> None:
        now = self.clock.monotonic()
        self._results = OrderedDict(
            (key, result) for key, result in self._results.items() if now - result.finished < 1800
        )

    def invalidate(self) -> None:
        with self._lock:
            self._results.clear()

    def import_dates(self, scan_id: str, dates: list[str], operation_id: str) -> dict[str, object]:
        parameters: dict[str, object] = {"scanId": scan_id, "dates": sorted(set(dates))}

        def save(work: UnitOfWork, at: Instant) -> dict[str, object]:
            with self._lock:
                self._prune()
                result = self._results.get(scan_id)
                if result is None:
                    raise DomainError("HISTORY_SCAN_EXPIRED", "历史扫描不存在或已过期，请重新扫描", "scan_id")
                if any(day not in result.dates for day in dates):
                    raise DomainError("INVALID_REQUEST", "所选日期不在扫描结果内", "dates")
                selected = tuple(event for day in sorted(set(dates)) for event in result.dates[day])
            return self.store.import_events(work, selected, roots=result.roots, at=at)

        result = execute(self.store, self.clock, "history.import", parameters, operation_id, save)
        # Retry also drains a committed inbox if the previous process stopped before billing.
        self.billing.run()
        return result
