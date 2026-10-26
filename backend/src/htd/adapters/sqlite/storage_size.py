"""Logical snapshot and physical files have separate, explicitly sampled sizes."""

import os
import sqlite3
import stat
from pathlib import Path
from threading import Lock
from time import monotonic

from htd.domain.common.time import Instant, format_instant


def tree_bytes(root: Path) -> int:
    """Stream only owned trees, without following links or retaining file inventories."""
    total = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            info = directory.stat(follow_symlinks=False)
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                continue
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if (
                        stat.S_ISLNK(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    ):
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode):
                        total += info.st_size
        except FileNotFoundError:
            continue
    return total


def footprint(db: sqlite3.Connection, *, at: Instant) -> dict[str, object]:
    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    page = db.execute("PRAGMA page_size").fetchone()[0]
    allocated = db.execute("PRAGMA page_count").fetchone()[0] * page
    free = db.execute("PRAGMA freelist_count").fetchone()[0] * page

    def size(item: Path) -> int:
        try:
            return item.stat().st_size
        except FileNotFoundError:
            return 0

    main, wal, shm = (size(item) for item in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")))
    backups, migration = (tree_bytes(path.parent / name) for name in ("backups", "migration"))
    total = main + wal + shm + backups + migration
    total += sum(tree_bytes(path.parent / name) for name in ("logs", "webview"))
    total += sum(size(path.parent / name) for name in (".htd-dataset.json", ".htd-directory-move.json"))
    return {
        "mainFileBytes": main,
        "walBytes": wal,
        "shmBytes": shm,
        "allocatedBytes": allocated,
        "freePageBytes": free,
        "usedPageBytes": allocated - free,
        "backupBytes": backups,
        "migrationBytes": migration,
        "totalBytes": total,
        "sampledAt": format_instant(at),
    }


class StorageSampler:
    """One lazy fifteen-minute sample; normal status reads never poll the filesystem."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._cached: dict[str, object] | None = None
        self._key: object = None
        self._expires = 0.0

    def sample(self, db: sqlite3.Connection, *, at: Instant, key: object) -> dict[str, object]:
        with self._lock:
            if self._cached is None or self._key != key or monotonic() >= self._expires:
                self._cached = footprint(db, at=at)
                self._key = key
                self._expires = monotonic() + 15 * 60
            return dict(self._cached)

    def clear(self) -> None:
        with self._lock:
            self._cached = None
