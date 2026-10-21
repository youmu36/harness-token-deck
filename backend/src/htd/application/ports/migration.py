"""Migration I/O stays behind a port supplied by the composition root."""

from typing import Protocol

from htd.application.ports.management import TaskContext


class MigrationEngine(Protocol):
    def detect(self, source_path: str | None = None) -> dict[str, object]: ...

    def report_path(self, migration_id: str) -> str: ...

    def run(
        self, migration_id: str, context: TaskContext, source_path: str | None = None
    ) -> dict[str, object]: ...

    def activate(self, migration_id: str, operation_id: str) -> dict[str, object]: ...
