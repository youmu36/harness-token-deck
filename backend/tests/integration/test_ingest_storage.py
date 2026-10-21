"""Validate bounded billing, corrupt-inbox diagnostics and atomic rollback at every billing boundary."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from htd.adapters.sqlite.connection import open_connection
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.usage_codec import dumps, encode_event, object_json
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htd.application.ports import BilledUsage
from htd.application.ports import UnitOfWork as WorkPort
from htd.domain.usage.model import UsageEvent

from .collection_helpers import AT, collect, quote, usage


def test_billing_restart_and_repeated_collection_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        collect(writer, (usage(1), usage(2)))
    with WriteExecutor(path) as writer:
        result = writer.execute(lambda work: IngestRepository(work.connection).bill_batch(work, quote, at=AT))
        assert result.billed == 2 and not result.more
        repeated = collect(writer, (usage(1), usage(2)), offset=200)
        assert repeated.inserted == 0 and repeated.duplicates == 2
        empty = writer.execute(lambda work: IngestRepository(work.connection).bill_batch(work, quote, at=AT))
        assert empty.processed == 0
    with closing(open_connection(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM identity WHERE billed=1").fetchone()[0] == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("raw", "invalid SECRET BODY"),
        ("raw", '{"schema":1,"schema":1}'),
        ("schema", 2),
        ("schema", True),
        ("harness", "dsh"),
        ("dedupe_key", f"{99:064x}"),
        ("occurred_at", "2026-09-30T01:00:00Z"),
        ("model_id", {"secret": "SECRET BODY"}),
        ("tokens", {"input": -1, "cache_read": 0, "cache_write": 0, "output": 1, "reasoning": 0}),
        ("issues", "SECRET BODY"),
        ("time_precision", "unknown"),
    ],
)
def test_corrupt_inbox_is_logged_processed_and_never_reingested(
    tmp_path: Path, field: str, value: object
) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        collect(writer, (usage(1),))
        data = object_json(encode_event(usage(1)))
        data[field] = value
        raw = str(value) if field == "raw" else dumps(data)
        writer.execute(lambda work: work.connection.execute("UPDATE inbox SET event_json=?", (raw,)))
        result = writer.execute(lambda work: IngestRepository(work.connection).bill_batch(work, quote, at=AT))
        assert result.corrupt == 1 and result.billed == 0 and not result.more
        assert collect(writer, (usage(1),)).duplicates == 1
    with closing(open_connection(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
        assert connection.execute("SELECT billed FROM identity").fetchone()[0] == 1
        log = connection.execute("SELECT code,detail FROM diagnostic_log").fetchone()
        assert log[0] == "inbox_corrupt" and "SECRET" not in log[1]


def test_identity_timestamp_mismatch_is_corrupt(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        collect(writer, (usage(1),))
        writer.execute(
            lambda work: work.connection.execute("UPDATE identity SET occurred_at='2026-09-30T01:00:00.000Z'")
        )
        result = writer.execute(lambda work: IngestRepository(work.connection).bill_batch(work, quote, at=AT))
        assert result.corrupt == 1 and result.billed == 0


@pytest.mark.parametrize("table,verb", [("ledger", "INSERT"), ("identity", "UPDATE"), ("inbox", "DELETE")])
def test_billing_failure_rolls_back_ledger_identity_and_inbox(tmp_path: Path, table: str, verb: str) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        collect(writer, (usage(1), usage(2)))
        writer.execute(
            lambda work: work.connection.execute(
                f"CREATE TRIGGER fail_write BEFORE {verb} ON {table} "
                f"WHEN NEW.dedupe_key=X'{usage(2).dedupe_key}' BEGIN SELECT RAISE(ABORT,'injected'); END"
                if verb != "DELETE"
                else f"CREATE TRIGGER fail_write BEFORE DELETE ON {table} "
                f"WHEN OLD.dedupe_key=X'{usage(2).dedupe_key}' BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            writer.execute(lambda work: IngestRepository(work.connection).bill_batch(work, quote, at=AT))
    with closing(open_connection(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM identity WHERE billed=1").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 2
        assert connection.execute("SELECT value FROM revision WHERE scope='usage'").fetchone()[0] == 0


def test_wallet_callback_and_corrupt_log_share_the_billing_transaction(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        collect(writer, (usage(1), usage(2)))
        writer.execute(
            lambda work: work.connection.execute(
                "UPDATE inbox SET event_json='broken' WHERE dedupe_key=?", (key_bytes(usage(1).dedupe_key),)
            )
        )

        def bill(work: UnitOfWork) -> None:
            def wallet(_: WorkPort, priced: BilledUsage) -> None:
                work.connection.execute(
                    "INSERT INTO preference VALUES ('wallet_probe',?)", (priced.event.dedupe_key,)
                )
                raise ValueError("wallet failed")

            IngestRepository(work.connection).bill_batch(work, quote, at=AT, on_billed=wallet)

        with pytest.raises(ValueError, match="wallet failed"):
            writer.execute(bill)
    with closing(open_connection(path)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM preference WHERE key='wallet_probe'").fetchone()[0] == 0
        )
        for table in ("ledger", "diagnostic_log"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM identity WHERE billed=1").fetchone()[0] == 0


def test_deferred_billing_keeps_inbox_and_revision(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        collect(writer, (usage(1),))
        result = writer.execute(
            lambda work: IngestRepository(work.connection).bill_batch(work, lambda _: None, at=AT)
        )
        assert result.processed == 0 and result.more
    with closing(open_connection(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 1
        assert connection.execute("SELECT billed FROM identity").fetchone()[0] == 0
        assert connection.execute("SELECT value FROM revision WHERE scope='usage'").fetchone()[0] == 0


def test_batch_has_500_row_limit_and_order(tmp_path: Path) -> None:
    seen: list[str] = []

    def calculate(event: UsageEvent) -> BilledUsage:
        seen.append(event.dedupe_key)
        return quote(event)

    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        collect(writer, tuple(usage(i) for i in range(520, 0, -1)))
        first = writer.execute(
            lambda work: IngestRepository(work.connection).bill_batch(work, calculate, at=AT)
        )
        assert first.billed == 500 and first.more
        second = writer.execute(
            lambda work: IngestRepository(work.connection).bill_batch(work, calculate, at=AT)
        )
        assert second.billed == 20 and not second.more
    assert seen == [usage(i).dedupe_key for i in range(1, 521)]


@pytest.mark.parametrize("limit", [0, -1, 501, True])
def test_invalid_billing_limit(tmp_path: Path, limit: int) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer, pytest.raises(ValueError, match="limit"):
        writer.execute(
            lambda work: IngestRepository(work.connection).bill_batch(work, quote, at=AT, limit=limit)
        )


def test_billing_callback_cannot_replace_event(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        collect(writer, (usage(1),))

        def wrong(event: UsageEvent) -> BilledUsage:
            return quote(replace(event, model_id="different"))

        with pytest.raises(ValueError, match="preserve"):
            writer.execute(lambda work: IngestRepository(work.connection).bill_batch(work, wrong, at=AT))
    with closing(open_connection(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0


@pytest.mark.parametrize("billed_flag", [0, 1])
def test_stale_inbox_is_removed_without_calling_billing_again(tmp_path: Path, billed_flag: int) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        collect(writer, (usage(1),))
        writer.execute(lambda work: IngestRepository(work.connection).bill_batch(work, quote, at=AT))

        def stale(work: UnitOfWork) -> None:
            work.connection.execute("UPDATE identity SET billed=?", (billed_flag,))
            work.connection.execute(
                "INSERT INTO inbox VALUES (?,?,?,?,?)",
                (
                    "codex",
                    key_bytes(usage(1).dedupe_key),
                    "2026-09-30T00:00:00.000Z",
                    encode_event(usage(1)),
                    "2026-09-30T00:00:00.000Z",
                ),
            )

        writer.execute(stale)

        def unexpected(_: UsageEvent) -> BilledUsage:
            raise AssertionError("Duplicate must not be billed again")

        result = writer.execute(
            lambda work: IngestRepository(work.connection).bill_batch(work, unexpected, at=AT)
        )
        assert result.duplicates == 1 and result.billed == 0 and not result.more
    with closing(open_connection(path)) as connection:
        assert connection.execute("SELECT billed FROM identity").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 1


def test_retained_billed_identity_prevents_rebilling_after_ledger_cleanup(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        collect(writer, (usage(1),))
        writer.execute(lambda work: IngestRepository(work.connection).bill_batch(work, quote, at=AT))
        writer.execute(lambda work: work.connection.execute("DELETE FROM ledger"))
        assert collect(writer, (usage(1),)).duplicates == 1
    with closing(open_connection(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0


def test_repository_write_requires_active_owner_transaction(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with (
        WriteExecutor(path),
        closing(open_connection(path)) as connection,
        pytest.raises(RuntimeError, match="transaction"),
    ):
        IngestRepository(connection).bill_batch(UnitOfWork(connection), quote, at=AT)
