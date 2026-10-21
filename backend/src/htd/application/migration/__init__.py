"""Start migration rehearsals with durable task receipts and explicit activation."""

from typing import cast
from uuid import uuid4

from htd.application.command import execute
from htd.application.ports import CommandStore, TaskControl, UnitOfWork
from htd.application.ports.migration import MigrationEngine
from htd.domain.common.time import Clock, Instant


class Migration:
    def __init__(
        self, engine: MigrationEngine, store: CommandStore, tasks: TaskControl, clock: Clock
    ) -> None:
        self.engine, self.store, self.tasks, self.clock = engine, store, tasks, clock

    def detect(self, source_path: str | None = None) -> dict[str, object]:
        return self.engine.detect(source_path)

    def run(self, operation_id: str, source_path: str | None = None) -> dict[str, object]:
        reserved: list[str] = []

        def reserve(work: UnitOfWork, at: Instant) -> dict[str, object]:
            task_id = self.tasks.reserve()
            reserved.append(task_id)
            migration_id = str(uuid4())
            return {
                "taskId": task_id,
                "migrationId": migration_id,
                "reportPath": self.engine.report_path(migration_id),
            }

        try:
            parameters: dict[str, object] = {"dryRun": True}
            if source_path is not None:
                parameters["sourcePath"] = source_path
            result = execute(self.store, self.clock, "migration.run", parameters, operation_id, reserve)
        except BaseException:
            for task_id in reserved:
                self.tasks.discard(task_id)
            raise
        if reserved:
            self.tasks.start(
                cast(str, result["taskId"]),
                lambda context: self.engine.run(cast(str, result["migrationId"]), context, source_path),
            )
        return result

    def activate(self, migration_id: str, operation_id: str) -> dict[str, object]:
        return self.engine.activate(migration_id, operation_id)
