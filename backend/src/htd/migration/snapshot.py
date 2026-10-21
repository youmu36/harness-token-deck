"""Create a consistent SQLite backup and bind its freezing time to a SHA-256 digest."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from htd.adapters.sqlite.usage_codec import dumps, object_json
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.migration.legacy.reader import backup, require_legacy_stopped


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    source: Path
    path: Path
    sha256: str
    at: Instant


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(
    source: Path, destination: Path, *, at: Instant | None = None, detector: Callable[[], bool] | None = None
) -> SnapshotResult:
    require_legacy_stopped(detector=detector)
    source, destination = source.resolve(), destination.resolve()
    metadata_path = destination.with_suffix(".json")
    if source == destination or destination.exists() or metadata_path.exists():
        raise DomainError("MIGRATION_DESTINATION_EXISTS", "快照目标已存在，请选择新的迁移目录")
    destination.parent.mkdir(parents=True, exist_ok=True)
    at = at or Instant(datetime.now(UTC))
    with destination.open("xb"):
        pass
    try:
        backup(source, destination)
        digest = sha256_file(destination)
        metadata = {"source": str(source), "sha256": digest, "snapshotAt": format_instant(at)}
        with metadata_path.open("x", encoding="utf-8") as stream:
            stream.write(dumps(metadata) + "\n")
        return SnapshotResult(source, destination, digest, at)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def snapshot_time(path: Path, fallback: str) -> Instant:
    metadata_path = path.with_suffix(".json")
    if not metadata_path.exists():
        return parse_instant(fallback)
    try:
        metadata = object_json(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("sha256") != sha256_file(path):
            raise ValueError("Snapshot digest changed")
        return parse_instant(str(metadata["snapshotAt"]))
    except (KeyError, ValueError) as error:
        raise DomainError("MIGRATION_SNAPSHOT_INVALID", "快照元数据或 SHA-256 无效") from error
