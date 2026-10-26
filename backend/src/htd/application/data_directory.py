"""Application entry for data location management."""

from htd.application.ports.data_directory import DataDirectoryPort


class DataDirectory:
    def __init__(self, store: DataDirectoryPort) -> None:
        self.store = store

    def status(self) -> dict[str, object]:
        return self.store.status()

    def preview(self, target: str) -> dict[str, object]:
        return self.store.preview(target)

    def move(self, target: str, fingerprint: str, operation_id: str) -> dict[str, object]:
        return self.store.move(target, fingerprint, operation_id)

    def cleanup(self, operation_id: str) -> dict[str, object]:
        return self.store.cleanup(operation_id)
