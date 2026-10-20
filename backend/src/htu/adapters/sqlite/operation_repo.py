"""Persist command receipts and reject cross-command or changed-parameter operation reuse."""

import sqlite3

from htu.adapters.sqlite.transaction import require_transaction
from htu.adapters.sqlite.usage_codec import dumps, object_json
from htu.application.ports import UnitOfWork
from htu.domain.common.errors import OPERATION_ID_REUSED, DomainError
from htu.domain.common.time import Instant, format_instant
from htu.domain.wallet.model import validate_identifier


class OperationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def replay(
        self, operation_id: str, command: str, parameters: dict[str, object]
    ) -> dict[str, object] | None:
        validate_identifier(operation_id, "operation_id")
        row = self.connection.execute(
            "SELECT command,result_json FROM operation WHERE id=?", (operation_id,)
        ).fetchone()
        if row is None:
            return None
        receipt = object_json(row["result_json"])
        if row["command"] != command or receipt.get("parameters") != parameters:
            raise DomainError(OPERATION_ID_REUSED, "操作标识已用于其他命令或参数", "operation_id")
        return object_json(dumps(receipt["result"]))

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
        require_transaction(work, self.connection)
        self.connection.execute(
            "INSERT INTO operation VALUES (?,?,?,?)",
            (operation_id, command, dumps({"parameters": parameters, "result": result}), format_instant(at)),
        )
