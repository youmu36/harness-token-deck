"""Scheduler completion, cooperative stops and fallback diagnostics against real temporary SQLite."""

import sqlite3
from collections.abc import Callable, Iterable
from concurrent.futures import Future
from pathlib import Path
from threading import Condition, Event, Thread

import pytest
from htd.adapters.collectors.claude_reader import ClaudeReader
from htd.adapters.collectors.codex_reader import CodexReader
from htd.adapters.collectors.discovery import Discovery
from htd.adapters.collectors.dsh_reader import DshReader
from htd.adapters.collectors.zcode_reader import ZcodeReader
from htd.adapters.sqlite.collection_store import SqliteCollectionStore
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.scheduler_store import SqliteSchedulerStore
from htd.adapters.sqlite.sources_repo import SourcesRepository
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.adapters.sqlite.writer import WriteExecutor
from htd.adapters.watch.watcher import Watcher
from htd.application.collection.billing import BillInbox
from htd.application.collection.scan import ScanRound
from htd.application.ports import (
    CollectionCompleted,
    CommitBatchResult,
    SourceRecord,
    UnitOfWork,
    UsageChanged,
    WalletChanged,
)
from htd.domain.common.errors import DomainError
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import Instant
from htd.domain.usage.model import Harness, UsageEvent
from htd.jobs.scheduler import Scheduler

from .scan_helpers import AT, count, headers, request, scanner, seed_prices, sessions


def wait_for_notification(condition: Condition, timeout: float) -> None:
    condition.wait()


class AdvancingWait:
    def __init__(self) -> None:
        self._changed = Condition()
        self._condition: Condition | None = None
        self._sequence = 0

    def __call__(self, condition: Condition, timeout: float) -> None:
        with self._changed:
            self._condition = condition
            self._sequence += 1
            self._changed.notify_all()
        condition.wait()

    def parked(self, after: int = 0) -> None:
        with self._changed:
            assert self._changed.wait_for(lambda: self._sequence > after, timeout=2)

    def advance(self, clock: FakeClock, milliseconds: int) -> None:
        sequence = self._sequence
        assert self._condition is not None
        with self._condition:
            clock.advance(milliseconds)
            self._condition.notify_all()
        self.parked(sequence)


class AvailableWatcher:
    def refresh(self, sources: tuple[SourceRecord, ...], notify: Callable[[], None]) -> bool:
        return True

    def close(self, timeout: float = 1) -> None:
        pass


def test_manual_future_observes_committed_ledger_wallet_and_completed_event(tmp_path: Path) -> None:
    path, home = tmp_path / "ledger.sqlite", tmp_path / "home"
    file = sessions(home) / "one.jsonl"
    file.write_bytes(headers() + request(1, AT))
    published: list[object] = []
    snapshots: list[tuple[int, int, int]] = []

    def publish(event: object) -> None:
        if isinstance(event, CollectionCompleted):
            with sqlite3.connect(path) as db:
                snapshots.append(
                    (
                        db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0],
                        db.execute("SELECT COUNT(*) FROM wallet_entry").fetchone()[0],
                        db.execute("SELECT value FROM revision WHERE scope='sources'").fetchone()[0],
                    )
                )
        published.append(event)

    clock = FakeClock(AT)
    with WriteExecutor(path, publish=publish) as writer, ReadPool(path) as pool:
        writer.execute(seed_prices)
        writer.execute(
            lambda w: WalletRepository(w.connection).create_vendor(
                w, vendor_id="vendor", name="Shop", at=AT, operation_id="vendor"
            )
        )
        writer.execute(lambda w: WalletRepository(w.connection).link_group(w, "g", "vendor", at=AT))
        scheduler = Scheduler(
            scanner(writer, pool, home, clock),
            SqliteSchedulerStore(writer, pool),
            clock,
            watcher=AvailableWatcher(),
            wait=wait_for_notification,
        )
        future = scheduler.scan_now()  # Attach before start to cover the startup round deterministically.
        scheduler.start()
        try:
            result = future.result(2)
            assert result.trigger == "manual" and result.result.billing.batch.billed == 1
            assert [type(e) for e in published] == [
                UsageChanged,
                UsageChanged,
                WalletChanged,
                CollectionCompleted,
            ]
            assert snapshots == [(1, 1, result.revision)]
            assert count(writer, "inbox") == 0
            assert scheduler.scan_now().result(2).result.billing.batch.billed == 0
            assert count(writer, "ledger") == count(writer, "wallet_entry") == 1
        finally:
            scheduler.close(2)


def test_periodic_scan_collects_codex_append_without_watcher_notification(tmp_path: Path) -> None:
    path, home = tmp_path / "ledger.sqlite", tmp_path / "home"
    file = sessions(home) / "one.jsonl"
    file.write_bytes(headers())
    clock = FakeClock(AT)
    wait = AdvancingWait()
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        writer.execute(seed_prices)
        scan = scanner(writer, pool, home, clock)
        assert isinstance(scan.discovery, Discovery)
        scheduler = Scheduler(
            scan,
            SqliteSchedulerStore(writer, pool),
            clock,
            watcher=AvailableWatcher(),
            wait=wait,
            before_scan=scan.discovery.prepare,
        )
        startup = scheduler.scan_now()
        scheduler.start()
        try:
            assert startup.result(2).trigger == "manual"
            wait.parked()
            # Keep the writer open across several polling periods, as Codex does.
            with file.open("ab", buffering=0) as stream:
                for number in range(1, 5):
                    stream.write(request(number, clock.now_utc()))
                    wait.advance(clock, 30_000)
                    assert count(writer, "ledger") == number
                    assert count(writer, "inbox") == 0
                    assert scheduler.status().last_completed_at == clock.now_utc()
                partial = request(5, clock.now_utc())
                stream.write(partial[:-1])
                wait.advance(clock, 30_000)
                assert count(writer, "ledger") == 4
                stream.write(b"\n")
                wait.advance(clock, 30_000)
                assert count(writer, "ledger") == 5
        finally:
            scheduler.close(2)


def test_polling_diagnostic_persists_all_sources_and_clears_after_recovery(tmp_path: Path) -> None:
    path, home = tmp_path / "ledger.sqlite", tmp_path / "home"
    sessions(home)
    clock = FakeClock(AT)
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        runtime = SqliteSchedulerStore(writer, pool)
        scheduler = Scheduler(scanner(writer, pool, home, clock), runtime, clock, wait=wait_for_notification)
        future = scheduler.scan_now()
        scheduler.start()
        try:
            future.result(2)
            sources = runtime.sources()
            assert len(sources) == len(Harness)
            assert all("watcher_unavailable_polling" in s.diagnostics for s in sources)
            assert sources[0].status == "degraded"
            revision = writer.execute(lambda w: w.revision("sources"))
            runtime.watcher_status(False)
            assert writer.execute(lambda w: w.revision("sources")) == revision
            runtime.watcher_status(True)
            recovered = runtime.sources()
            assert all("watcher_unavailable_polling" not in s.diagnostics for s in recovered)
            assert recovered[0].status == "watching"
            assert recovered[1].diagnostics == ("source_database_unavailable",)
            assert [(s.started_at, s.last_scan_at) for s in sources] == [
                (s.started_at, s.last_scan_at) for s in recovered
            ]
        finally:
            scheduler.close(2)


def test_paused_state_survives_database_and_scheduler_restart(tmp_path: Path) -> None:
    path, home = tmp_path / "ledger.sqlite", tmp_path / "home"
    (sessions(home) / "one.jsonl").write_bytes(headers() + request(1, AT))
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        runtime = SqliteSchedulerStore(writer, pool)
        runtime.set_paused(True)
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        clock = FakeClock(AT)
        runtime = SqliteSchedulerStore(writer, pool)
        scheduler = Scheduler(scanner(writer, pool, home, clock), runtime, clock, wait=wait_for_notification)
        scheduler.start()
        try:
            with pytest.raises(DomainError) as failure:
                scheduler.scan_now().result(2)
            assert failure.value.code == "COLLECTION_PAUSED"
            assert count(writer, "identity") == 0
            scheduler.set_paused(False)
            scheduler.scan_now().result(2)
            assert count(writer, "inbox") == 1 and not runtime.paused()
        finally:
            scheduler.close(2)


class BlockingStore(SqliteCollectionStore):
    def __init__(self, writer: WriteExecutor, pool: ReadPool) -> None:
        super().__init__(writer, pool)
        self.entered, self.release = Event(), Event()

    def commit_batch(
        self,
        work: UnitOfWork,
        harness: Harness,
        root: str,
        path: str,
        checkpoint: dict[str, object],
        events: Iterable[UsageEvent],
        *,
        at: Instant,
    ) -> CommitBatchResult:
        result = super().commit_batch(work, harness, root, path, checkpoint, events, at=at)
        self.entered.set()
        assert self.release.wait(2)
        return result


class PausingRuntime(SqliteSchedulerStore):
    def __init__(self, writer: WriteExecutor, pool: ReadPool) -> None:
        super().__init__(writer, pool)
        self.requested = Event()

    def set_paused(self, paused: bool) -> None:
        self.requested.set()
        super().set_paused(paused)


@pytest.mark.parametrize("stop", ["pause", "close"])
def test_stop_during_transaction_a_commits_current_file_and_skips_next_batch(
    tmp_path: Path, stop: str
) -> None:
    path, home = tmp_path / "ledger.sqlite", tmp_path / "home"
    root = sessions(home)
    for number in (1, 2):
        (root / f"{number}.jsonl").write_bytes(headers() + request(number, AT))
    clock = FakeClock(AT)
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        writer.execute(seed_prices)
        store = BlockingStore(writer, pool)
        runtime = PausingRuntime(writer, pool)
        scan = ScanRound(
            store,
            Discovery(home=home, environ={}),
            {
                Harness.CLAUDE: ClaudeReader(),
                Harness.CODEX: CodexReader(),
                Harness.ZCODE: ZcodeReader(),
                Harness.DSH: DshReader(),
            },
            BillInbox(store, clock),
            clock,
        )
        scheduler = Scheduler(scan, runtime, clock, wait=wait_for_notification)
        future = scheduler.scan_now()
        scheduler.start()
        pause_result: Future[None] = Future()

        def pause() -> None:
            try:
                scheduler.set_paused(True)
                pause_result.set_result(None)
            except Exception as error:
                pause_result.set_exception(error)

        pause_thread: Thread | None = None
        try:
            assert store.entered.wait(2)
            if stop == "pause":
                pause_thread = Thread(target=pause)
                pause_thread.start()
                assert runtime.requested.wait(2)
            else:
                with pytest.raises(DomainError) as failure:
                    scheduler.close(0)
                assert failure.value.code == "COLLECTION_CLOSE_TIMEOUT"
            store.release.set()
            if pause_thread is not None:
                pause_result.result(2)
                pause_thread.join(2)
            with pytest.raises(DomainError) as failure:
                future.result(2)
            assert failure.value.code == ("COLLECTION_PAUSED" if stop == "pause" else "COLLECTION_STOPPED")
            scheduler.close(2)
            assert count(writer, "identity") == count(writer, "inbox") == 1
            assert count(writer, "ledger") == 0
            codex = SourcesRepository(pool.get()).get(Harness.CODEX)
            assert codex is not None and codex.started_at == AT
            assert SourcesRepository(pool.get()).get(Harness.ZCODE) is None
            runtime.set_paused(False)
            recovered = scanner(writer, pool, home, clock).run()
            assert recovered.billing.batch.billed == 2
            assert count(writer, "identity") == count(writer, "ledger") == 2
        finally:
            store.release.set()
            scheduler.close(2)
            if pause_thread is not None:
                pause_thread.join(2)


def test_billing_stop_between_transactions_keeps_remaining_inbox(tmp_path: Path) -> None:
    path, home = tmp_path / "ledger.sqlite", tmp_path / "home"
    (sessions(home) / "one.jsonl").write_bytes(headers() + request(1, AT) + request(2, AT))
    stopped = Event()

    def publish(event: object) -> None:
        if isinstance(event, UsageChanged):
            stopped.set()

    with WriteExecutor(path, publish=publish) as writer, ReadPool(path) as pool:
        writer.execute(seed_prices)
        scan = scanner(writer, pool, home, FakeClock(AT), batch_size=1)
        scan.collect()
        stopped.clear()  # Acceptance has its own committed usage event.
        result = scan.billing.run(should_stop=stopped.is_set)
        assert result.batch.billed == 1 and result.batch.more
        assert count(writer, "inbox") == 1
        assert scan.billing.run().batch.billed == 1
        assert count(writer, "ledger") == 2


def test_cancelled_queued_billing_task_never_starts_a_new_batch(tmp_path: Path) -> None:
    path, home = tmp_path / "ledger.sqlite", tmp_path / "home"
    (sessions(home) / "one.jsonl").write_bytes(headers() + request(1, AT))
    entered, queued, release, cancelled = Event(), Event(), Event(), Event()

    class QueuedStore(SqliteCollectionStore):
        def execute[T](self, function: Callable[[UnitOfWork], T]) -> T:
            queued.set()
            return super().execute(function)

    def block(work: object) -> None:
        entered.set()
        assert release.wait(2)

    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        writer.execute(seed_prices)
        scanner(writer, pool, home, FakeClock(AT)).collect()
        blocker = writer.submit(block)
        assert entered.wait(2)
        billing = BillInbox(QueuedStore(writer, pool), FakeClock(AT))
        result: Future[object] = Future()

        def bill() -> None:
            try:
                result.set_result(billing.run(should_stop=cancelled.is_set))
            except Exception as error:
                result.set_exception(error)

        thread = Thread(target=bill)
        thread.start()
        try:
            assert queued.wait(2)
            cancelled.set()
            release.set()
            blocker.result(2)
            result.result(2)
            assert count(writer, "ledger") == 0 and count(writer, "inbox") == 1
        finally:
            release.set()
            thread.join(2)


def test_watchdog_observes_temporary_zcode_wal_and_stops(tmp_path: Path) -> None:
    notified = Event()
    watcher = Watcher()
    source = SourceRecord(Harness.ZCODE, str(tmp_path / "db.sqlite"), None, None, "pending", ())
    try:
        assert watcher.refresh((source,), notified.set)
        (tmp_path / "db.sqlite-wal").write_bytes(b"synthetic WAL notification")
        assert notified.wait(2)
    finally:
        watcher.close()
