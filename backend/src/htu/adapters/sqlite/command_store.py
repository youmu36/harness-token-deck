"""Consistent command snapshots and atomic preference/source receipts on the unique writer."""

import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager

from htu.adapters.sqlite.connection import ReadPool
from htu.adapters.sqlite.ledger_read import LedgerReader
from htu.adapters.sqlite.maintenance_repo import MaintenanceRepository
from htu.adapters.sqlite.operation_repo import OperationRepository
from htu.adapters.sqlite.sources_repo import SourcesRepository
from htu.adapters.sqlite.transaction import require_transaction
from htu.adapters.sqlite.usage_codec import dumps, loads
from htu.adapters.sqlite.writer import UnitOfWork as SqlWork
from htu.adapters.sqlite.writer import WriteExecutor
from htu.application.ports import PreferenceSnapshot, SourcesSnapshot, UnitOfWork
from htu.domain.common.time import Instant
from htu.domain.usage.model import Harness


def connection(work: UnitOfWork) -> sqlite3.Connection:
    if not isinstance(work, SqlWork):
        raise TypeError("Requires SQLite UnitOfWork")
    require_transaction(work, work.connection)
    return work.connection


@contextmanager
def read_snapshot(pool: ReadPool) -> Generator[sqlite3.Connection, None, None]:
    with pool.connection() as db:
        db.execute("BEGIN")
        yield db


def _preferences(db: sqlite3.Connection) -> PreferenceSnapshot:
    values = {
        row[0]: loads(row[1])
        for row in db.execute(
            "SELECT key,value_json FROM preference "
            "WHERE key NOT IN ('retention','desktopWindow','legacyFilters','au"
            "toMatchProvider','longTermRetention')"
        )
    }
    wallet_scope = db.execute("SELECT value FROM runtime_state WHERE key='wallet_default_scope'").fetchone()
    values["walletDefaultScope"] = wallet_scope[0] if wallet_scope else "local"
    revision = db.execute("SELECT value FROM revision WHERE scope='preferences'").fetchone()[0]
    return PreferenceSnapshot(values, revision)


def _sources(db: sqlite3.Connection, *, details: bool = True) -> SourcesSnapshot:
    repo = SourcesRepository(db)
    return SourcesSnapshot(
        tuple(source for h in Harness if (source := repo.get(h)) is not None),
        LedgerReader(db).source_counts() if details else (),
        db.execute("SELECT value FROM revision WHERE scope='sources'").fetchone()[0],
        MaintenanceRepository(db).state("collection_paused") == "true",
    )


class SqliteCommandStore:
    def __init__(self, writer: WriteExecutor, readers: ReadPool) -> None:
        self.writer, self.readers = writer, readers

    def execute[T](self, action: Callable[[UnitOfWork], T]) -> T:
        return self.writer.execute(action)

    def revisions(self) -> dict[str, int]:
        with self.readers.connection() as db:
            return dict(db.execute("SELECT scope,value FROM revision"))

    def preferences(self, work: UnitOfWork | None = None) -> PreferenceSnapshot:
        if work is not None:
            return _preferences(connection(work))
        with read_snapshot(self.readers) as db:
            return _preferences(db)

    def save_preferences(self, work: UnitOfWork, values: dict[str, object]) -> None:
        connection(work).executemany(
            "INSERT INTO preference VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
            ((key, dumps(value)) for key, value in values.items() if key != "walletDefaultScope"),
        )
        # Released binaries expose every preference key through their strict DTO.
        # Keep this extension outside that table so 0.4.1 can still read preferences.
        connection(work).execute(
            "INSERT INTO runtime_state VALUES('wallet_default_scope',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (values.get("walletDefaultScope", "local"),),
        )

    def sources(self, work: UnitOfWork | None = None, *, details: bool = True) -> SourcesSnapshot:
        if work is not None:
            return _sources(connection(work), details=details)
        with read_snapshot(self.readers) as db:
            return _sources(db, details=details)

    def set_path(self, work: UnitOfWork, harness: Harness, path: str, *, available: bool) -> None:
        SourcesRepository(connection(work)).set_path(work, harness, path, available=available)

    def save_paused(self, work: UnitOfWork, paused: bool) -> None:
        if not isinstance(work, SqlWork):
            raise TypeError("Requires SQLite UnitOfWork")
        repo = MaintenanceRepository(connection(work))
        value = "true" if paused else "false"
        if repo.state("collection_paused") != value:
            repo.set_state(work, "collection_paused", value)
            work.touch("sources")

    def replay(
        self, work: UnitOfWork, operation_id: str, command: str, parameters: dict[str, object]
    ) -> dict[str, object] | None:
        return OperationRepository(connection(work)).replay(operation_id, command, parameters)

    def record(
        self,
        work: UnitOfWork,
        operation_id: str,
        command: str,
        parameters: dict[str, object],
        result: dict[str, object],
        *,
        at: Instant,
    ) -> None:
        OperationRepository(connection(work)).record(work, operation_id, command, parameters, result, at=at)
