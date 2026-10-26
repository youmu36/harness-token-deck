"""Bridge thread churn cannot retain one database/page cache per transient caller."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.writer import WriteExecutor


def test_existing_maintenance_expires_idle_readers_without_touching_a_borrowed_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr("htd.adapters.sqlite.connection.time.monotonic", lambda: now[0])
    with (
        WriteExecutor(tmp_path / "ledger.sqlite3") as writer,
        ReadPool(writer.path) as pool,
        pool.connection() as active,
    ):
        with pool.connection() as idle:
            assert idle.execute("SELECT 1").fetchone()[0] == 1
        now[0] = 31
        pool.expire_idle()
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            idle.execute("SELECT 1")
        assert active.execute("SELECT 1").fetchone()[0] == 1


def test_many_threads_share_four_scoped_readers_and_hidden_trim_keeps_active_reads(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "ledger.sqlite3") as writer, ReadPool(writer.path) as pool:
        arrived, proceed = Barrier(5), Event()

        def read(index: int) -> int:
            with pool.connection() as db:
                db.execute("BEGIN")
                if index < 4:
                    arrived.wait(timeout=2)
                    assert proceed.wait(timeout=2)
                assert db.execute("PRAGMA cache_size").fetchone()[0] == -512
                assert db.execute("PRAGMA query_only").fetchone()[0] == 1
                return db.execute("SELECT ?", (index,)).fetchone()[0]

        with ThreadPoolExecutor(max_workers=16) as workers:
            first = [workers.submit(read, i) for i in range(4)]
            arrived.wait(timeout=2)
            waiting = [workers.submit(read, i) for i in range(4, 40)]
            assert pool.diagnostics()["connections"] == 4
            assert pool.diagnostics()["active"] == 4
            pool.release_idle()
            assert pool.diagnostics()["connections"] == 4
            proceed.set()
            assert [future.result(timeout=2) for future in first + waiting] == list(range(40))
        assert pool.diagnostics()["active"] == 0
        assert pool.diagnostics()["connections"] <= 4
        pool.release_idle()
        assert pool.diagnostics()["connections"] == 0
        with pool.connection() as db:
            assert not db.in_transaction
            assert db.execute("SELECT 1").fetchone()[0] == 1


def test_expired_idle_connection_is_replaced_and_dead_diagnostic_threads_are_reaped(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "ledger.sqlite3") as writer, ReadPool(writer.path, idle_seconds=0) as pool:
        with pool.connection() as first:
            first.execute("SELECT 1").fetchone()
        assert pool.diagnostics()["connections"] == 0
        with ThreadPoolExecutor(max_workers=1) as worker:
            worker.submit(lambda: pool.get().execute("SELECT 1").fetchone()[0]).result(timeout=2)
        assert pool.diagnostics()["legacy"] == 0


def test_unused_remote_provider_does_not_allocate_a_tls_client(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    import httpx
    from htd.adapters.providers.newapi import NewApiProvider
    from htd.domain.common.testing import FakeClock

    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("Idle provider allocated an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected)

    async def run() -> None:
        async with NewApiProvider(FakeClock()):
            pass

    asyncio.run(run())


def test_daily_pages_are_reused_until_wallet_revision_or_timezone_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    import htd.adapters.sqlite.wallet_command_store as module
    from htd.adapters.sqlite.wallet_entries import DailySpending

    from .test_wallet_storage import calibrate, consumption, vendor

    calls: list[int] = []
    original = module.wallet_entries

    def counted(
        db: sqlite3.Connection,
        vendor_id: str,
        cursor: int | None,
        limit: int,
        kind: str,
        offset: int,
        *,
        daily: DailySpending | None = None,
    ) -> dict[str, object]:
        calls.append(1)
        return original(db, vendor_id, cursor, limit, kind, offset, daily=daily)

    monkeypatch.setattr(module, "wallet_entries", counted)
    with WriteExecutor(tmp_path / "ledger.sqlite3") as writer, ReadPool(writer.path) as readers:
        writer.execute(lambda w: vendor(w, initial="100"))
        writer.execute(consumption)
        store = module.SqliteWalletCommandStore(writer, readers)
        first = store.wallet_entries("vendor", None, 50)
        assert store.wallet_entries("vendor", None, 50) == first and len(calls) == 1
        writer.execute(calibrate)
        assert store.wallet_entries("vendor", None, 50) != first and len(calls) == 2
        shifted = store.wallet_entries("vendor", None, 50, offset_minutes=-210)
        assert len(calls) == 3
        store.release_cache()
        assert store.wallet_entries("vendor", None, 50, offset_minutes=-210) == shifted
        assert len(calls) == 4
