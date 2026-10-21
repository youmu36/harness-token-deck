"""Logical snapshot and physical files have separate, explicitly sampled sizes."""

import sqlite3
from pathlib import Path

from htd.domain.common.time import Instant, format_instant


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

    return {
        "mainFileBytes": size(path),
        "walBytes": size(Path(str(path) + "-wal")),
        "shmBytes": size(Path(str(path) + "-shm")),
        "allocatedBytes": allocated,
        "freePageBytes": free,
        "usedPageBytes": allocated - free,
        "backupBytes": sum(size(item) for item in (path.parent / "backups").glob("*.sqlite3")),
        "migrationBytes": sum(size(item) for item in (path.parent / "migration").glob("*/*.sqlite3")),
        "sampledAt": format_instant(at),
    }
