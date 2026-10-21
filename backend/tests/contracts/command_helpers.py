"""Temporary application setup and typed assertions for the implemented command subset."""

from collections.abc import Iterable, Mapping

from htd.adapters.sqlite.key_codec import key_bytes
from htd.bootstrap import Application
from htd.domain.common.time import format_instant
from htd.domain.statistics import LedgerRow
from htd.ipc.dto.base import Error, Failure, StrictModel


def invoke[T: StrictModel](
    app: Application, command: str, payload: Mapping[str, object], response: type[T]
) -> T:
    result = app.router.call(command, payload)
    assert result["ok"], result
    return response.model_validate(result["data"])


def failure(app: Application, command: str, payload: Mapping[str, object]) -> Error:
    return Failure.model_validate(app.router.call(command, payload)).error


def seed_rows(app: Application, rows: Iterable[LedgerRow]) -> None:
    # Synthetic ledger projections isolate statistics from source/pricing behavior.
    from htd.adapters.sqlite.writer import UnitOfWork

    def insert(work: UnitOfWork) -> None:
        work.connection.executemany(
            "INSERT INTO ledger(harness,dedupe_key,occurred_at,session_id,request_id,model_id,model_key,"
            "display_name,status,input,cache_read,output,issues_json,rule_applied,"
            "pico,incomplete,unpriced,billed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'none',?,?,?,?)",
            (
                (
                    row.harness.value,
                    key_bytes(row.dedupe_key),
                    format_instant(row.occurred_at),
                    row.session_id,
                    row.dedupe_key,
                    row.model_id,
                    row.model_key,
                    row.display_name,
                    "completed" if row.harness.value == "zcode" else "reported",
                    row.input,
                    row.cache_read,
                    row.output,
                    "[]",
                    str(row.pico_usd),
                    int(row.incomplete),
                    int(row.unpriced),
                    format_instant(row.occurred_at),
                )
                for row in rows
            ),
        )
        work.touch("usage")

    app.writer.execute(insert)
