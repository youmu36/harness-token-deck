"""Resolve the one application dataset before logging, SQLite or WebView2 starts."""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

type LocationSource = Literal["explicit", "environment", "locator", "default"]
MARKER = ".htd-dataset.json"


def is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


class DataDirectoryError(RuntimeError):
    """Startup must expose this path/recovery error instead of opening an empty dataset."""


@dataclass(frozen=True)
class DataLocation:
    root: Path
    source: LocationSource
    locator: Path
    dataset_id: str | None = None
    phase: str = "active"


def locator_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    local = env.get("LOCALAPPDATA")
    return (Path(local) if local else Path.home() / "AppData/Local") / "HarnessTokenDeck.location.json"


def local_path(value: str | Path) -> Path:
    raw = str(value)
    if raw.startswith(("\\\\", "//")):
        raise DataDirectoryError("数据目录须位于本地磁盘，不能使用网络路径")
    return Path(raw).expanduser().resolve()


def read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if type(value) is not dict:
            raise ValueError("not an object")
        return cast(dict[str, object], value)
    except (OSError, ValueError) as error:
        raise DataDirectoryError(
            f"无法读取数据定位或身份文件: {path}; 请恢复该文件或显式指定原数据目录"
        ) from error


def atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def dataset_id(root: Path) -> str:
    value = read_json(root / MARKER)
    try:
        identifier = value["datasetId"]
        if value.get("format") != 1 or type(identifier) is not str or str(UUID(identifier)) != identifier:
            raise ValueError("invalid dataset marker")
        return identifier
    except (KeyError, ValueError, TypeError) as error:
        raise DataDirectoryError(f"数据集身份损坏: {root}; 请恢复身份文件，不要合并不同账本") from error


def resolve_data_directory(
    explicit: Path | None = None, *, environ: Mapping[str, str] | None = None
) -> DataLocation:
    env = os.environ if environ is None else environ
    locator = locator_path(env)
    if explicit is not None:
        return DataLocation(local_path(explicit), "explicit", locator)
    if env.get("HTD_DATA_DIR"):
        return DataLocation(local_path(env["HTD_DATA_DIR"]), "environment", locator)
    if not locator.exists():
        return DataLocation(locator.parent / "HarnessTokenDeck", "default", locator)
    record = read_json(locator)
    try:
        if record.get("format") != 1 or record.get("phase") not in {"committed", "active", "deleted"}:
            raise ValueError("unsupported locator format/phase")
        raw, identifier = record["root"], record["datasetId"]
        if type(raw) is not str or not Path(raw).is_absolute() or type(identifier) is not str:
            raise ValueError("invalid locator fields")
        root = local_path(raw)
    except (KeyError, TypeError, ValueError) as error:
        raise DataDirectoryError(f"启动定位损坏或阶段未知: {locator}; 请恢复定位记录") from error
    if record["phase"] == "deleted":
        if (root / "htd.sqlite3").exists():
            raise DataDirectoryError("显式删除后的目录又出现数据库，请先核对恢复来源")
        return DataLocation(root, "locator", locator, None, "deleted")
    if not root.is_dir() or not (root / "htd.sqlite3").is_file():
        raise DataDirectoryError(f"已选择的数据目录不可用: {root}; 请重新连接磁盘或恢复此数据集")
    if dataset_id(root) != identifier:
        raise DataDirectoryError(f"数据集身份与启动定位不一致: {root}; 已停止启动以防账本分叉")
    if record["phase"] == "committed":
        from htd.adapters.sqlite.dataset_snapshot import fingerprint

        journal = read_json(root / ".htd-directory-move.json")
        if journal.get("tables") != fingerprint(root / "htd.sqlite3"):
            raise DataDirectoryError(f"迁移目标校验失败: {root}; 请保留两份数据并恢复已验证副本")
    return DataLocation(root, "locator", locator, identifier, str(record["phase"]))


def ensure_dataset(location: DataLocation) -> str:
    """Only the selected root is initialized; overrides never modify the user locator."""
    location.root.mkdir(parents=True, exist_ok=True)
    if (location.root / MARKER).exists():
        identifier = dataset_id(location.root)
        marker = read_json(location.root / MARKER)
        if marker.get("moveTarget") and marker.get("locator"):
            record_path = Path(str(marker["locator"]))
            if record_path.exists():
                record = read_json(record_path)
                if record.get("datasetId") == identifier and record.get("root") == marker["moveTarget"]:
                    raise DataDirectoryError(f"此数据集已迁移到 {marker['moveTarget']}; 请从新目录启动")
    else:
        identifier = str(uuid4())
        atomic_json(location.root / MARKER, {"format": 1, "datasetId": identifier})
    if location.dataset_id is not None and location.dataset_id != identifier:
        raise DataDirectoryError(f"数据集身份已变化: {location.root}")
    return identifier
