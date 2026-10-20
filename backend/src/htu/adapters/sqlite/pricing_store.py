"""Coherent management snapshots and pricing writes on the shared executor."""

import sqlite3

from htu.adapters.sqlite.command_store import SqliteCommandStore, connection, read_snapshot
from htu.adapters.sqlite.models_repo import ModelsRepository
from htu.adapters.sqlite.pricing_repo import PricingRepository
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.application.ports import PricingSnapshot, UnitOfWork
from htu.domain.common.time import Instant
from htu.domain.pricing.catalog import Catalog
from htu.domain.pricing.model_pool import ModelPool


class SqlitePricingStore(SqliteCommandStore):
    def pricing(self, work: UnitOfWork | None = None) -> PricingSnapshot:
        if work is not None:
            return self._pricing(connection(work))
        with read_snapshot(self.readers) as db:
            return self._pricing(db)

    @staticmethod
    def _pricing(db: sqlite3.Connection) -> PricingSnapshot:
        return PricingSnapshot(
            PricingRepository(db).load(),
            frozenset(
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT price_group_id FROM ledger WHERE price_group_id IS NOT NULL"
                )
            ),
            frozenset(row[0] for row in db.execute("SELECT DISTINCT group_id FROM recalc_batch")),
            WalletRepository(db).links(),
        )

    def save_catalog(self, work: UnitOfWork, old: Catalog, new: Catalog) -> None:
        from htu.adapters.sqlite.writer import UnitOfWork as SqlWork

        if not isinstance(work, SqlWork):
            raise TypeError("Requires SQLite UnitOfWork")
        PricingRepository(connection(work)).save(work, old, new)

    def models(self, work: UnitOfWork | None = None) -> tuple[ModelPool, int]:
        if work is not None:
            return ModelsRepository(connection(work)).load(), work.revision("models")
        with read_snapshot(self.readers) as db:
            return ModelsRepository(db).load(), db.execute(
                "SELECT value FROM revision WHERE scope='models'"
            ).fetchone()[0]

    def save_models(self, work: UnitOfWork, old: ModelPool, new: ModelPool, *, at: Instant) -> None:
        from htu.adapters.sqlite.writer import UnitOfWork as SqlWork

        if not isinstance(work, SqlWork):
            raise TypeError("Requires SQLite UnitOfWork")
        ModelsRepository(connection(work)).save(work, old, new, at=at)
