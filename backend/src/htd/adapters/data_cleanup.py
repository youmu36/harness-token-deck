"""Explicit uninstall cleanup removes only application-owned paths, never the selected root tree."""

import shutil
from pathlib import Path

from htd.adapters.data_directory import (
    MARKER,
    DataDirectoryError,
    DataLocation,
    atomic_json,
    is_link,
    read_json,
)
from htd.adapters.sqlite.dataset_snapshot import is_native_dataset


def remove_application_data(location: DataLocation) -> None:
    root = location.root.resolve()
    if not root.exists():
        return
    if is_link(root) or root == Path(root.anchor):
        raise DataDirectoryError("不能清理链接或磁盘根目录")
    database = root / "htd.sqlite3"
    if database.exists() and not is_native_dataset(database):
        raise DataDirectoryError("无法确认此数据库属于本应用，已保留全部文件")
    trees = [root / name for name in ("backups", "migration", "logs", "webview")]
    for tree in trees:
        if is_link(tree) or not tree.resolve().is_relative_to(root):
            raise DataDirectoryError("应用子目录指向其他位置，已停止清理")
        if tree.exists() and any(is_link(path) for path in tree.rglob("*")):
            raise DataDirectoryError("应用子目录包含链接，已停止清理")
    for name in ("htd.sqlite3", "htd.sqlite3-wal", "htd.sqlite3-shm", MARKER, ".htd-directory-move.json"):
        path = root / name
        if is_link(path) or not path.resolve().is_relative_to(root):
            raise DataDirectoryError("应用文件指向其他位置，已停止清理")
    for tree in trees:
        if tree.is_dir():
            shutil.rmtree(tree)
    for name in ("htd.sqlite3", "htd.sqlite3-wal", "htd.sqlite3-shm", MARKER, ".htd-directory-move.json"):
        (root / name).unlink(missing_ok=True)
    if location.source == "locator":
        record = read_json(location.locator)
        record["phase"] = "deleted"
        record.pop("previousRoot", None)
        atomic_json(location.locator, record)
    if not any(root.iterdir()):
        root.rmdir()
