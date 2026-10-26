"""Recoverable copy/verify/commit for a single dataset; never merge directories."""

import hashlib
import json
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast
from uuid import uuid4

from htd.adapters.data_directory import (
    MARKER,
    DataDirectoryError,
    DataLocation,
    atomic_json,
    dataset_id,
    is_link,
    local_path,
    read_json,
)
from htd.adapters.sqlite.dataset_snapshot import fingerprint as dataset_fingerprint
from htd.adapters.sqlite.dataset_snapshot import snapshot
from htd.domain.common.errors import DomainError

JOURNAL = ".htd-directory-move.json"
OWNED_TREES = ("backups", "migration", "logs")


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def owned_files(root: Path) -> Iterator[Path]:
    for name in ("htd.sqlite3", "htd.sqlite3-wal", "htd.sqlite3-shm"):
        path = root / name
        if path.is_file() and not is_link(path):
            yield path
    for name in OWNED_TREES:
        tree = root / name
        if is_link(tree):
            raise DomainError("DATA_DIRECTORY_LINK", "应用数据子目录不能是链接")
        if tree.is_dir():
            for path in tree.rglob("*"):
                if is_link(path):
                    raise DomainError("DATA_DIRECTORY_LINK", "应用数据中存在链接，请先处理后再迁移")
                if path.is_file():
                    yield path


class DataDirectoryManager:
    def __init__(
        self,
        location: DataLocation,
        *,
        quiesce: Callable[[], None],
        resume: Callable[[], None],
        committed: Callable[[], None],
    ) -> None:
        self.location = location
        self.quiesce, self.resume, self.committed = quiesce, resume, committed

    def status(self) -> dict[str, object]:
        location = self.location
        previous: str | None = None
        cleanup = False
        if location.source == "locator":
            record = read_json(location.locator)
            previous = cast(str | None, record.get("previousRoot"))
            cleanup = record.get("phase") == "active" and previous is not None
        journal = location.root / JOURNAL
        progress = read_json(journal) if journal.exists() else {}
        phase = str(progress.get("phase", "active"))
        return {
            "root": str(location.root),
            "source": location.source,
            "phase": phase,
            "previousRoot": previous,
            "canMove": location.source in {"default", "locator"},
            "canCleanup": cleanup,
            "pendingTarget": progress.get("target") if phase in {"copying", "verified"} else None,
        }

    def preview(self, target: str) -> dict[str, object]:
        if self.location.source not in {"default", "locator"}:
            raise DomainError("DATA_DIRECTORY_OVERRIDE", "当前路径由外部指定，请移除外部覆盖后再迁移")
        try:
            if not Path(target).is_absolute():
                raise DataDirectoryError("请选择绝对路径")
            destination = local_path(target)
        except DataDirectoryError as error:
            raise DomainError("DATA_DIRECTORY_INVALID", str(error)) from error
        source = self.location.root
        unchanged = source == destination
        resume_operation: str | None = None
        if not unchanged and (destination.is_relative_to(source) or source.is_relative_to(destination)):
            raise DomainError("DATA_DIRECTORY_NESTED", "原目录和目标目录不能互相包含")
        if not unchanged and destination.exists() and any(destination.iterdir()):
            journal = destination / JOURNAL
            if not journal.is_file() or read_json(journal).get("datasetId") != dataset_id(source):
                raise DomainError("DATA_DIRECTORY_NOT_EMPTY", "目标目录已有文件或其他账本，请选择空目录")
            record = read_json(journal)
            if record.get("source") != str(source) or record.get("phase") not in {"copying", "verified"}:
                raise DomainError("DATA_DIRECTORY_TARGET_ACTIVE", "目标目录已有已切换数据，不能覆盖")
            resume_operation = str(record["operationId"])
        existing = destination
        while not existing.exists():
            existing = existing.parent
        probe = existing / (".htd-write-probe-" + uuid4().hex)
        try:
            with probe.open("xb"):
                pass
        except OSError as error:
            raise DomainError("DATA_DIRECTORY_PERMISSION", "目标目录不可写，请检查权限或磁盘状态") from error
        finally:
            probe.unlink(missing_ok=True)
        required = sum(path.stat().st_size for path in owned_files(source)) + 16 * 1024**2
        available = shutil.disk_usage(existing).free
        if available < required:
            raise DomainError("DATA_DIRECTORY_SPACE", "目标磁盘空间不足，无法保留完整数据和校验副本")
        payload = {"source": str(source), "target": str(destination), "datasetId": dataset_id(source)}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return {
            **payload,
            "requiredBytes": required,
            "availableBytes": available,
            "unchanged": unchanged,
            "fingerprint": digest,
            "resumeOperationId": resume_operation,
        }

    def move(self, target: str, fingerprint: str, operation_id: str) -> dict[str, object]:
        if self.location.locator.exists():
            prior = read_json(self.location.locator)
            if prior.get("operationId") == operation_id:
                if prior.get("fingerprint") != fingerprint or prior.get("root") != str(local_path(target)):
                    raise DomainError("OPERATION_ID_REUSED", "同一操作号不能用于不同迁移参数")
                return {"root": str(prior["root"]), "restartRequired": True}
        preview = self.preview(target)
        if preview["fingerprint"] != fingerprint:
            raise DomainError("DATA_DIRECTORY_PLAN_CHANGED", "目录已变化，请重新预检")
        if preview["unchanged"]:
            return {"root": str(self.location.root), "restartRequired": False}
        source, destination = self.location.root, Path(str(preview["target"]))
        identifier = dataset_id(source)
        journal: dict[str, object] = {
            "format": 1,
            "source": str(source),
            "target": str(destination),
            "datasetId": identifier,
            "operationId": operation_id,
            "phase": "copying",
        }
        self.quiesce()
        switched = False
        try:
            destination.mkdir(parents=True, exist_ok=True)
            current = destination / JOURNAL
            if current.exists():
                old = read_json(current)
                if old.get("operationId") != operation_id:
                    raise DomainError("DATA_DIRECTORY_OPERATION", "上次迁移尚未结束，请沿用原操作重试")
                existing_db = destination / "htd.sqlite3"
                if existing_db.exists() and dataset_fingerprint(existing_db) != old.get("tables"):
                    raise DomainError("DATA_DIRECTORY_TARGET_CHANGED", "目标账本已变化，不能覆盖")
            atomic_json(current, journal)
            atomic_json(source / JOURNAL, journal)
            temporary = destination / "htd.sqlite3.moving"
            temporary.unlink(missing_ok=True)  # Only our journal-owned staging file.
            facts = snapshot(source / "htd.sqlite3", temporary)
            journal["tables"] = facts
            atomic_json(current, journal)
            manifest: list[dict[str, str]] = []
            for path in owned_files(source):
                relative = path.relative_to(source)
                if relative.parts[0] != "logs":
                    manifest.append({"path": relative.as_posix(), "sha256": file_hash(path)})
                if relative.parts[0].startswith("htd.sqlite3"):
                    continue
                copy = destination / relative
                copy.parent.mkdir(parents=True, exist_ok=True)
                if copy.exists():
                    if file_hash(copy) != file_hash(path):
                        raise DomainError(
                            "DATA_DIRECTORY_COPY_CONFLICT", "目标已有不同内容的文件，已停止迁移"
                        )
                else:
                    with path.open("rb") as reader, copy.open("xb") as writer:
                        shutil.copyfileobj(reader, writer, length=1024 * 1024)
                    shutil.copystat(path, copy)
                if file_hash(copy) != file_hash(path):
                    raise RuntimeError("Copied file checksum mismatch")
            temporary.replace(destination / "htd.sqlite3")
            atomic_json(destination / MARKER, {"format": 1, "datasetId": identifier})
            journal.update({"phase": "verified", "manifest": manifest, "tables": facts})
            atomic_json(current, journal)
            atomic_json(source / JOURNAL, journal)
            # Source fence is durable before committing. An uncommitted fence still permits recovery there.
            atomic_json(
                source / MARKER,
                {
                    "format": 1,
                    "datasetId": identifier,
                    "moveTarget": str(destination),
                    "locator": str(self.location.locator),
                },
            )
            atomic_json(
                self.location.locator,
                {
                    "format": 1,
                    "root": str(destination),
                    "datasetId": identifier,
                    "phase": "committed",
                    "previousRoot": str(source),
                    "operationId": operation_id,
                    "fingerprint": fingerprint,
                },
            )
            switched = True
            self.committed()
            journal["phase"] = "committed"
            atomic_json(current, journal)
            return {"root": str(destination), "restartRequired": True}
        finally:
            if not switched:
                self.resume()

    def activated(self) -> None:
        if self.location.source != "locator":
            return
        record = read_json(self.location.locator)
        if record.get("phase") in {"committed", "deleted"}:
            record["phase"] = "active"
            record["datasetId"] = dataset_id(self.location.root)
            atomic_json(self.location.locator, record)
            journal = self.location.root / JOURNAL
            if journal.is_file():
                state = read_json(journal)
                state["phase"] = "active"
                atomic_json(journal, state)

    def cleanup(self, operation_id: str) -> dict[str, object]:
        if self.location.locator.exists():
            receipt = read_json(self.location.locator)
            if receipt.get("cleanupOperationId") == operation_id:
                return {"deletedFiles": receipt["cleanupDeletedFiles"]}
        state = self.status()
        if not state["canCleanup"]:
            raise DomainError("DATA_DIRECTORY_CLEANUP_UNAVAILABLE", "尚未确认新目录启动，不能清理旧副本")
        old = local_path(str(state["previousRoot"]))
        if old == self.location.root or old.is_relative_to(self.location.root):
            raise DomainError("DATA_DIRECTORY_CLEANUP_INVALID", "旧副本路径不安全")
        if dataset_id(old) != dataset_id(self.location.root):
            raise DomainError("DATA_DIRECTORY_CLEANUP_IDENTITY", "旧副本身份已变化，不能自动清理")
        journal = read_json(self.location.root / JOURNAL)
        manifest = cast(list[dict[str, str]], journal["manifest"])
        paths: list[Path] = []
        for item in manifest:
            path = old / item["path"]
            if is_link(path) or not path.resolve().is_relative_to(old) or path == old:
                raise DomainError("DATA_DIRECTORY_CLEANUP_INVALID", "清理清单包含不安全路径")
            if path.exists():
                if file_hash(path) != item["sha256"]:
                    raise DomainError("DATA_DIRECTORY_OLD_CHANGED", "旧副本内容发生变化，已保留供手动核对")
                paths.append(path)
        for path in paths:
            path.unlink()
        record = read_json(self.location.locator)
        record.pop("previousRoot", None)
        record.update({"cleanupOperationId": operation_id, "cleanupDeletedFiles": len(paths)})
        atomic_json(self.location.locator, record)
        return {"deletedFiles": len(paths)}


def recover_unactivated_directory(locator: Path, original: Path) -> None:
    """Explicit recovery is allowed only before the committed target has ever activated."""
    record = read_json(locator)
    root = local_path(original)
    if record.get("phase") != "committed" or record.get("previousRoot") != str(root):
        raise DataDirectoryError("只能恢复尚未激活的迁移原目录; 新目录启动后请恢复最新完整备份")
    if dataset_id(root) != record.get("datasetId"):
        raise DataDirectoryError("恢复目录的数据集身份不一致")
    journal = read_json(root / JOURNAL)
    if dataset_fingerprint(root / "htd.sqlite3") != journal.get("tables"):
        raise DataDirectoryError("原副本已变化，不能作为未激活迁移的恢复来源")
    target = Path(str(record["root"]))
    if target.exists():
        atomic_json(
            target / MARKER,
            {"format": 1, "datasetId": record["datasetId"], "moveTarget": str(root), "locator": str(locator)},
        )
    record.update({"root": str(root), "phase": "active", "recoveredFrom": str(target)})
    record.pop("previousRoot", None)
    record.pop("operationId", None)
    atomic_json(locator, record)
    atomic_json(root / MARKER, {"format": 1, "datasetId": record["datasetId"]})
    journal["phase"] = "recovered"
    atomic_json(root / JOURNAL, journal)
