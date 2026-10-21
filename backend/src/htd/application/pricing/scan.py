"""Read-only fourteen-day model discovery with isolated historical checkpoints."""

from datetime import timedelta
from typing import cast

from htd.application.command import execute
from htd.application.ports import (
    CheckpointRecord,
    PricingStore,
    SourceDiscovery,
    SourceReader,
    TaskContext,
    TaskControl,
    UnitOfWork,
)
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Clock, Instant, format_instant, parse_instant
from htd.domain.pricing.model import model_name
from htd.domain.usage.model import Harness
from htd.domain.usage.model_key import model_key


class ModelScan:
    def __init__(
        self,
        store: PricingStore,
        discovery: SourceDiscovery,
        readers: dict[Harness, SourceReader],
        tasks: TaskControl,
        clock: Clock,
    ) -> None:
        self.store, self.discovery, self.readers, self.tasks, self.clock = (
            store,
            discovery,
            readers,
            tasks,
            clock,
        )

    def start(self, harness: str, operation_id: str) -> dict[str, object]:
        reserved: list[str] = []
        parameters: dict[str, object] = {"harness": harness}

        def reserve(work: UnitOfWork, at: Instant) -> dict[str, object]:
            task_id = self.tasks.reserve()
            reserved.append(task_id)
            return {"taskId": task_id}

        try:
            result = execute(self.store, self.clock, "models.scan", parameters, operation_id, reserve)
        except BaseException:
            for task_id in reserved:
                self.tasks.discard(task_id)
            raise
        if reserved:
            # Receipt commits before any task events can be published.
            self.tasks.start(cast(str, result["taskId"]), lambda context: self.run(harness, context))
        return result

    def run(self, harness: str, context: TaskContext) -> dict[str, object]:
        end = self.clock.now_utc()
        start = Instant(end.value - timedelta(days=3))
        sources = self.store.sources().sources
        files: list[tuple[Harness, str]] = []
        diagnostics: set[str] = set()
        for source in sources:
            context.check_cancelled()
            if harness != "all" and source.harness.value != harness:
                continue
            discovered = self.discovery.discover(source.harness, source.path)
            diagnostics.update(discovered.diagnostics)
            files.extend((source.harness, path) for path in discovered.paths)
        found: dict[str, str] = {}
        for index, (source, path) in enumerate(files):
            context.progress(index, len(files))
            checkpoint = CheckpointRecord()
            while True:
                context.check_cancelled()
                batch = self.readers[source].read(path, checkpoint, start)
                diagnostics.update(batch.diagnostics)
                for event in batch.events:
                    if event.model_id is None or not start <= parse_instant(event.occurred_at) < end:
                        continue
                    try:
                        name = model_name(event.model_id)
                    except DomainError:
                        continue
                    found.setdefault(model_key(name), name)
                    if len(found) > 5000:
                        raise DomainError("MODEL_POOL_LIMIT", "扫描发现超过 5000 个模型，请缩小来源范围")
                if not batch.more or batch.retry or not batch.available:
                    break
                if batch.checkpoint == checkpoint.state:
                    diagnostics.add("source_scan_stalled")
                    break
                checkpoint = CheckpointRecord(batch.checkpoint, batch.diagnostics)
        context.progress(len(files), len(files))
        pool, revision = self.store.models()
        existing, excluded = {model_key(name) for name in pool.models}, set(pool.excluded)
        return {
            "start": format_instant(start),
            "end": format_instant(end),
            "revision": revision,
            "models": [
                {"modelId": name, "inPool": key in existing, "excluded": key in excluded}
                for key, name in sorted(found.items())
            ],
            "diagnostics": sorted(diagnostics),
        }
