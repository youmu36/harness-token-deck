"""Identify and read all supported legacy tables through a query-only connection."""

import ctypes
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from htu.adapters.sqlite.usage_codec import object_json
from htu.domain.common.errors import DomainError

SQLiteConnection = sqlite3.Connection

REQUIRED = {
    "configuration",
    "collection_sources",
    "collection_checkpoints",
    "collection_inbox",
    "usage_identities",
    "usage_ledger",
    "usage_recalc_batches",
    "usage_recalc_items",
    "retention_runs",
    "runtime_state",
    "diagnostic_logs",
}


@dataclass(frozen=True, slots=True)
class LegacyVersion:
    version: str
    user_version: int
    storage_generation: int | None
    schema_name: str | None


@dataclass(frozen=True, slots=True)
class LegacySnapshot:
    path: Path
    identity: LegacyVersion
    tables: dict[str, tuple[dict[str, Any], ...]]

    def rows(self, table: str) -> tuple[dict[str, Any], ...]:
        return self.tables.get(table, ())

    def state(self, table: str) -> dict[str, Any]:
        rows = self.rows(table)
        if not rows:
            return {}
        try:
            return object_json(str(rows[0]["state_json"]))
        except (ValueError, RecursionError) as error:
            raise DomainError("LEGACY_CONFIGURATION_INVALID", "旧配置 JSON 无效", table) from error


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def identify(connection: sqlite3.Connection) -> LegacyVersion:
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    tables = _tables(connection)
    metadata = None
    if "schema_metadata" in tables:
        row = connection.execute(
            "SELECT storage_generation,current_schema FROM schema_metadata WHERE id=1"
        ).fetchone()
        if row is not None:
            metadata = (int(row[0]), str(row[1]))
    if user_version == 7:
        return LegacyVersion(
            "0.2.3", user_version, metadata[0] if metadata else None, metadata[1] if metadata else None
        )
    if user_version == 6 and metadata is not None:
        if metadata[0] != 6:
            raise DomainError("LEGACY_SCHEMA_UNSUPPORTED", "旧库 storage_generation 不受支持")
        return LegacyVersion("0.2.3~0.3.0", user_version, metadata[0], metadata[1])
    if user_version == 6 and not ({"schema_metadata", "configuration_extensions"} & tables):
        return LegacyVersion("0.2.2", user_version, None, None)
    raise DomainError("LEGACY_SCHEMA_UNSUPPORTED", "旧库 schema 版本不受支持")


def open_legacy(path: Path) -> sqlite3.Connection:
    """Open a legacy database read-only, including a busy timeout for a live source."""
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=2000")
    return connection


def _iter_tables(connection: sqlite3.Connection) -> Iterator[str]:
    preferred = (
        "configuration",
        "configuration_extensions",
        "schema_metadata",
        "collection_sources",
        "collection_checkpoints",
        "collection_inbox",
        "usage_identities",
        "usage_ledger",
        "usage_recalc_batches",
        "usage_recalc_items",
        "retention_runs",
        "runtime_state",
        "diagnostic_logs",
        "price_groups",
        "price_versions",
        "price_model_rates",
        "price_selections",
        "dsh_price_selections",
    )
    available = _tables(connection)
    yield from (table for table in preferred if table in available)


def read_legacy(path: Path) -> LegacySnapshot:
    with closing(open_legacy(path)) as connection:
        connection.execute("BEGIN")
        identity = identify(connection)
        missing = REQUIRED - _tables(connection)
        if missing:
            raise DomainError("LEGACY_SCHEMA_UNSUPPORTED", "旧库缺少必需表", ",".join(sorted(missing)))
        tables: dict[str, tuple[dict[str, Any], ...]] = {}
        for table in _iter_tables(connection):
            columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
            primary = sorted((row for row in columns if row[5]), key=lambda row: row[5])
            order = ",".join(str(row[1]) for row in primary) or "rowid"
            rows = connection.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
            tables[table] = tuple(dict(row) for row in rows)
        if len(tables["configuration"]) != 1 or tables["configuration"][0]["id"] != 1:
            raise DomainError("LEGACY_CONFIGURATION_INVALID", "旧库主配置缺失", "configuration")
        return LegacySnapshot(path.resolve(), identity, tables)


def legacy_running(*, detector: Callable[[], bool] | None = None) -> bool:
    """Check the old singleton mutex; tests can provide a detector without Win32 calls."""
    if detector is not None:
        return bool(detector())
    if os.name != "nt":
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenMutexW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    handle = kernel.OpenMutexW(0x00100000, False, "Local\\HarnessTokenUsageSingleton")
    if handle:
        kernel.CloseHandle(wintypes.HANDLE(cast(int, handle)))
        return True
    return ctypes.get_last_error() not in (2, 3)


def require_legacy_stopped(*, detector: Callable[[], bool] | None = None) -> None:
    if legacy_running(detector=detector):
        raise DomainError("LEGACY_RUNNING", "请先退出旧版 Harness Token Usage 再迁移")


def backup(source: Path, destination: Path) -> None:
    with closing(open_legacy(source)) as source_db, closing(sqlite3.connect(destination)) as target_db:
        source_db.backup(target_db)
        target_db.execute("PRAGMA journal_mode=DELETE")
