"""Require repository mutations to use the writer's active transaction."""

import sqlite3

from htu.adapters.sqlite.writer import UnitOfWork
from htu.application.ports import UnitOfWork as WorkPort


def require_transaction(work: WorkPort, connection: sqlite3.Connection) -> None:
    if not isinstance(work, UnitOfWork) or work.connection is not connection or not connection.in_transaction:
        raise RuntimeError("Repository writes require the owning writer transaction")
