"""Persist the model pool, exclusion list and editable base rates in the pricing transaction."""

import sqlite3

from htd.adapters.sqlite.writer import UnitOfWork
from htd.domain.common.errors import PRICING_REVISION_CONFLICT, DomainError
from htd.domain.common.time import Instant, format_instant
from htd.domain.pricing.model import PriceRates
from htd.domain.pricing.model_pool import BasePrice, ModelPool
from htd.domain.usage.model_key import model_key


class ModelsRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def load(self) -> ModelPool:
        own_transaction = not self.connection.in_transaction
        if own_transaction:
            self.connection.execute("BEGIN")
        try:
            models = tuple(
                row[0] for row in self.connection.execute("SELECT model_id FROM model_pool ORDER BY rowid")
            )
            excluded = tuple(
                row[0]
                for row in self.connection.execute("SELECT model_key FROM model_excluded ORDER BY model_key")
            )
            bases = tuple(
                BasePrice(
                    row["model_id"], PriceRates(row["input"], row["cache"], row["output"], row["cache_write"])
                )
                for row in self.connection.execute(
                    "SELECT b.*,p.model_id FROM model_base_rate b "
                    "JOIN model_pool p USING(model_key) ORDER BY b.model_key"
                )
            )
            return ModelPool(models, excluded, bases)
        finally:
            if own_transaction:
                self.connection.rollback()

    def save(self, work: UnitOfWork, old: ModelPool, new: ModelPool, *, at: Instant) -> None:
        if work.connection is not self.connection or not self.connection.in_transaction:
            raise RuntimeError("Model pool writes require the owning transaction")
        if self.load() != old:
            raise DomainError(PRICING_REVISION_CONFLICT, "模型池已更新，请刷新后重试")
        if old == new:
            return
        before = {model_key(name): name for name in old.models}
        after = {model_key(name): name for name in new.models}
        self.connection.executemany(
            "DELETE FROM model_pool WHERE model_key=?", ((key,) for key in before.keys() - after.keys())
        )
        for name in new.models:
            key = model_key(name)
            if key not in before:
                self.connection.execute(
                    "INSERT INTO model_pool VALUES (?,?,?)", (key, name, format_instant(at))
                )
            elif before[key] != name:
                self.connection.execute("UPDATE model_pool SET model_id=? WHERE model_key=?", (name, key))
        self.connection.execute("DELETE FROM model_excluded")
        self.connection.executemany(
            "INSERT INTO model_excluded VALUES (?,?,?)",
            ((key, key, format_instant(at)) for key in new.excluded),
        )
        self.connection.execute("DELETE FROM model_base_rate")
        self.connection.executemany(
            "INSERT INTO model_base_rate(model_key,input,cache,output,cache_write) VALUES (?,?,?,?,?)",
            (
                (
                    model_key(base.model_id),
                    base.rates.input,
                    base.rates.cache,
                    base.rates.output,
                    base.rates.cache_write,
                )
                for base in new.base_prices
            ),
        )
        work.touch("models")
