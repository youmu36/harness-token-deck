"""Small leased reader pool; query lifetimes, rather than bridge threads, own connections."""

import sqlite3
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from threading import Condition, Thread, current_thread
from types import TracebackType

from htd.adapters.sqlite.key_codec import key_row
from htd.adapters.sqlite.recalc_codec import equivalent


def _pico_add(a: str, b: str) -> str:
    return str(int(a) + int(b))


def _pico_sub(a: str, b: str) -> str:
    return str(int(a) - int(b))


def open_connection(
    path: Path, *, query_only: bool = False, cross_thread: bool = False
) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path, isolation_level=None, check_same_thread=not cross_thread, cached_statements=32
    )
    connection.row_factory = key_row
    try:

        def allow_group_delete(group_id: str) -> int:
            try:
                return int(
                    connection.execute(
                        "SELECT 1 FROM temp.allow_group_delete WHERE group_id=?", (group_id,)
                    ).fetchone()
                    is not None
                )
            except sqlite3.OperationalError:
                return 0

        connection.create_function("htd_allow_group_delete", 1, allow_group_delete)

        def allow_compaction() -> int:
            try:
                return int(connection.execute("SELECT 1 FROM temp.allow_compaction").fetchone() is not None)
            except sqlite3.OperationalError:
                return 0

        connection.create_function("htd_allow_compaction", 0, allow_compaction)
        connection.create_function("htd_pico_add", 2, _pico_add, deterministic=True)
        connection.create_function("htd_pico_sub", 2, _pico_sub, deterministic=True)
        connection.create_function("htd_audit_equivalent", 6, equivalent, deterministic=True)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA recursive_triggers=ON")
        connection.execute("PRAGMA temp_store=" + ("FILE" if query_only else "MEMORY"))
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA mmap_size=0")
        connection.execute("PRAGMA cache_size=" + ("-512" if query_only else "-2048"))
        if query_only:
            connection.execute("PRAGMA query_only=ON")
        else:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TEMP TABLE allow_group_delete(group_id TEXT PRIMARY KEY) STRICT")
            connection.execute("CREATE TEMP TABLE allow_compaction(enabled INTEGER) STRICT")
    except BaseException:
        connection.close()
        raise
    return connection


class ReadPool:
    def __init__(self, path: Path, *, capacity: int = 4, idle_seconds: float = 30) -> None:
        if not path.is_file():
            raise FileNotFoundError(path)
        if capacity < 1 or idle_seconds < 0:
            raise ValueError("Invalid read pool limits")
        self.path = path
        self.capacity, self.idle_seconds = capacity, idle_seconds
        self._idle: list[tuple[sqlite3.Connection, float]] = []
        self._active: set[sqlite3.Connection] = set()
        self._legacy: dict[Thread, sqlite3.Connection] = {}
        self._condition = Condition()
        self._closed = False

    def _prune(self, *, all_idle: bool = False) -> None:
        for thread in tuple(self._legacy):
            if not thread.is_alive():
                self._legacy.pop(thread).close()
        now = time.monotonic()
        keep: list[tuple[sqlite3.Connection, float]] = []
        for db, at in self._idle:
            if all_idle or now - at >= self.idle_seconds:
                db.close()
            else:
                keep.append((db, at))
        self._idle = keep

    def _take(self) -> sqlite3.Connection:
        while True:
            if self._closed:
                raise RuntimeError("Read pool is closed")
            self._prune()
            if self._idle:
                return self._idle.pop()[0]
            if len(self._active) + len(self._legacy) < self.capacity:
                return open_connection(self.path, query_only=True, cross_thread=True)
            self._condition.wait(timeout=1)

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        with self._condition:
            db = self._take()
            self._active.add(db)
        try:
            yield db
        finally:
            with self._condition:
                try:
                    db.rollback()
                except sqlite3.Error:
                    db.close()
                else:
                    if self._closed:
                        db.close()
                    else:
                        self._idle.append((db, time.monotonic()))
                self._active.remove(db)
                self._condition.notify_all()

    def get(self) -> sqlite3.Connection:
        """Compatibility for synchronous diagnostic tools; production uses connection()."""
        with self._condition:
            if self._closed:
                raise RuntimeError("Read pool is closed")
            self._prune()
            thread = current_thread()
            if thread not in self._legacy:
                # An unscoped diagnostic caller cannot wait for another unscoped caller.
                if not self._idle and len(self._legacy) + len(self._active) >= self.capacity:
                    raise RuntimeError("Use a scoped read connection when the pool is full")
                self._legacy[thread] = self._take()
            return self._legacy[thread]

    def release_idle(self) -> None:
        with self._condition:
            self._prune(all_idle=True)
            self._condition.notify_all()

    def expire_idle(self) -> None:
        """Enforce the idle lease limit even when no further query borrows a reader."""
        with self._condition:
            self._prune()
            self._condition.notify_all()

    def diagnostics(self) -> dict[str, int]:
        with self._condition:
            self._prune()
            return {
                "connections": len(self._idle) + len(self._active) + len(self._legacy),
                "active": len(self._active),
                "idle": len(self._idle),
                "legacy": len(self._legacy),
                "capacity": self.capacity,
                "pageCacheLimitKiB": self.capacity * 512,
            }

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            while self._active:
                self._condition.wait()
            self._prune(all_idle=True)
            for connection in self._legacy.values():
                connection.close()
            self._legacy.clear()

    def reopen(self) -> None:
        with self._condition:
            if not self._closed:
                raise RuntimeError("Close readers before reopening")
            self._closed = False

    def __enter__(self) -> "ReadPool":
        return self

    def __exit__(
        self, kind: type[BaseException] | None, value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()
