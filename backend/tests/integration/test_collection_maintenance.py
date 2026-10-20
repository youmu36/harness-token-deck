"""Automatic models, wallet repair, retention and rotation on temporary real databases."""

import sqlite3
from collections.abc import Callable, Generator
from concurrent.futures import Future
from datetime import timedelta
from pathlib import Path
from threading import Event, Thread

import pytest
from htu.adapters.sqlite.auto_maintenance_store import SqliteMaintenanceStore
from htu.adapters.sqlite.connection import ReadPool
from htu.adapters.sqlite.ingest_repo import IngestRepository
from htu.adapters.sqlite.maintenance_repo import MaintenanceRepository
from htu.adapters.sqlite.models_repo import ModelsRepository
from htu.adapters.sqlite.pricing_repo import PricingRepository
from htu.adapters.sqlite.scheduler_store import SqliteSchedulerStore
from htu.adapters.sqlite.sources_repo import SourcesRepository
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htu.application.collection.maintenance import AutoMaintenance, MaintenanceResult
from htu.application.ports import (
    CollectionCompleted,
    ModelsChanged,
    PricingChanged,
    RetentionBatch,
    RetentionPlan,
    UsageChanged,
    WalletChanged,
)
from htu.application.ports import UnitOfWork as WorkPort
from htu.domain.common.errors import DomainError
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import Instant, format_instant
from htu.domain.pricing.model import PriceRates
from htu.domain.pricing.model_pool import BasePrice, ModelPool, remove_model
from htu.domain.usage.model import Harness
from htu.domain.usage.model_key import model_key
from htu.jobs.scheduler import Scheduler

from backend.tests.unit.jobs.test_scheduler import FakeMonotonic, FakeWait

from .collection_helpers import AT, collect, ensure_source, quote, usage
from .scan_helpers import count, headers, request, scanner, seed_prices, sessions
from .test_maintenance_storage import OLD, seed
from .test_pricing_storage import create
from .test_wallet_storage import consumption, recharge, vendor


def save_pool(work: UnitOfWork, pool: ModelPool) -> None:
    repo = ModelsRepository(work.connection)
    repo.save(work, repo.load(), pool, at=AT)


def observed(writer: WriteExecutor, names: tuple[str, ...]) -> None:
    collect(writer, tuple(usage(i + 1, model=name) for i, name in enumerate(names)))
    writer.execute(lambda work: IngestRepository(work.connection).bill_batch(work, quote, at=AT))


def expire_pending(writer: WriteExecutor, *, index: int = 1000) -> None:
    root = ensure_source(writer)
    writer.execute(lambda w: SourcesRepository(w.connection).begin_scan(w, Harness.CODEX, root, at=OLD))
    collect(writer, (usage(index, at=OLD),))


def test_sync_respects_exclusions_base_rates_and_immutable_price_history(tmp_path: Path) -> None:
    published: list[object] = []
    with WriteExecutor(tmp_path / "ledger.sqlite", publish=published.append) as writer:
        writer.execute(create)
        pool = remove_model(
            ModelPool(("Kept", "Excluded"), (), (BasePrice("Kept", PriceRates("1", "2", "3")),)),
            "Excluded",
        )
        writer.execute(lambda w: save_pool(w, pool))
        observed(writer, ("KEPT", " Excluded ", "New", "NEW"))
        fingerprint = writer.execute(lambda w: PricingRepository(w.connection).load().fingerprint)
        revision = writer.execute(lambda w: w.revision("models"))
        maintenance = AutoMaintenance(SqliteMaintenanceStore(writer), FakeClock(AT))
        result = maintenance.run(more=True)
        updated = writer.execute(lambda w: ModelsRepository(w.connection).load())
        assert result.models_added == 1 and not result.failed
        assert {model_key(n) for n in updated.models} == {"kept", "new"}
        assert updated.excluded == ("excluded",) and updated.base_prices == pool.base_prices
        assert writer.execute(lambda w: PricingRepository(w.connection).load().fingerprint) == fingerprint
        assert writer.execute(lambda w: w.revision("models")) == revision + 1
        assert published == [ModelsChanged(revision + 1)]
        assert maintenance.run(more=True).models_added == 0
        assert writer.execute(lambda w: w.revision("models")) == revision + 1


def test_capacity_adds_last_slot_logs_once_daily_and_skips_invalid_models(tmp_path: Path) -> None:
    class Observations(SqliteMaintenanceStore):
        def observed_models(self, work: WorkPort) -> Generator[str, None, None]:
            yield from ("", "x" * 161, "Excluded", "NewA", "newa", "NewB")

    with WriteExecutor(tmp_path / "ledger.sqlite") as writer:
        pool = ModelPool(tuple(f"Pool{i:04}" for i in range(4999)), ("excluded",))
        writer.execute(lambda w: save_pool(w, pool))
        clock = FakeClock(AT)
        maintenance = AutoMaintenance(Observations(writer), clock)
        result = maintenance.run(more=True)
        updated = writer.execute(lambda w: ModelsRepository(w.connection).load())
        assert result.models_added == 1 and result.pool_full
        assert len(updated.models) == 5000 and updated.models[-1] == "NewA"
        assert maintenance.run(more=True).pool_full
        assert count(writer, "diagnostic_log") == 1
        clock.advance(86_400_000)
        assert maintenance.run(more=True).pool_full
        assert count(writer, "diagnostic_log") == 2
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT code FROM diagnostic_log ORDER BY id").fetchall()
            )[0][0]
            == "model_pool_full"
        )


def test_full_pool_with_only_existing_or_excluded_names_has_no_capacity_diagnostic(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "ledger.sqlite") as writer:
        writer.execute(
            lambda w: save_pool(w, ModelPool(tuple(f"Pool{i}" for i in range(5000)), ("excluded",)))
        )
        observed(writer, ("Pool1", "Excluded"))
        result = AutoMaintenance(SqliteMaintenanceStore(writer), FakeClock(AT)).run(more=True)
        assert result.models_added == 0 and not result.pool_full
        assert count(writer, "diagnostic_log") == 0


def test_pool_and_capacity_diagnostic_rollback_together_then_retry(tmp_path: Path) -> None:
    published: list[object] = []
    with WriteExecutor(tmp_path / "ledger.sqlite", publish=published.append) as writer:
        writer.execute(lambda w: save_pool(w, ModelPool(tuple(f"Pool{i}" for i in range(4999)))))
        observed(writer, ("NewA", "NewB"))
        writer.execute(
            lambda w: w.connection.execute(
                "CREATE TRIGGER fail BEFORE INSERT ON diagnostic_log WHEN NEW.code='model_pool_full' "
                "BEGIN SELECT RAISE(ABORT,'private injected detail'); END"
            )
        )
        revision = writer.execute(lambda w: w.revision("models"))
        maintenance = AutoMaintenance(SqliteMaintenanceStore(writer), FakeClock(AT))
        result = maintenance.run(more=True)
        assert result.failed == ("models",) and result.models_added == 0
        assert len(writer.execute(lambda w: ModelsRepository(w.connection).load()).models) == 4999
        assert writer.execute(lambda w: w.revision("models")) == revision
        assert not published
        assert writer.execute(
            lambda w: MaintenanceRepository(w.connection).state("last_log_rotation_at")
        ) == format_instant(AT)
        detail = writer.execute(
            lambda w: w.connection.execute("SELECT detail FROM diagnostic_log").fetchone()[0]
        )
        assert "private injected detail" not in detail
        writer.execute(lambda w: w.connection.execute("DROP TRIGGER fail"))
        assert maintenance.run(more=True).models_added == 1
        assert published == [ModelsChanged(revision + 1)]


def test_daily_wallet_check_repairs_from_vouchers_only_when_due(tmp_path: Path) -> None:
    published: list[object] = []
    with WriteExecutor(tmp_path / "ledger.sqlite", publish=published.append) as writer:
        writer.execute(lambda w: vendor(w, initial="50"))
        writer.execute(recharge)
        writer.execute(consumption)
        writer.execute(lambda w: WalletRepository(w.connection).validate_rebuild(w, at=AT))
        expected = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
        revision = writer.execute(lambda w: w.revision("wallet"))
        writer.execute(lambda w: w.connection.execute("UPDATE wallet_total SET r='1',c='2',a='3',last_seq=0"))
        vouchers = count(writer, "wallet_entry")
        clock = FakeClock(AT)
        maintenance = AutoMaintenance(SqliteMaintenanceStore(writer), clock)
        clock.advance(86_399_000)
        assert not maintenance.run(more=True).wallets_repaired
        clock.advance(1000)
        assert maintenance.run(more=True).wallets_repaired == ("vendor",)
        repaired = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
        assert (repaired.r, repaired.c, repaired.a, repaired.b) == (
            expected.r,
            expected.c,
            expected.a,
            expected.b,
        )
        assert count(writer, "wallet_entry") == vouchers
        assert published == [WalletChanged(("vendor",), revision + 1)]
        assert not maintenance.run(more=True).wallets_repaired
        assert len(published) == 1


def test_more_defers_retention_but_still_rotates_logs_and_syncs_models(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "ledger.sqlite") as writer:
        writer.execute(seed)
        expire_pending(writer)
        writer.execute(lambda w: MaintenanceRepository(w.connection).log(w, "old", "expired", at=OLD))
        maintenance = AutoMaintenance(SqliteMaintenanceStore(writer), FakeClock(AT))
        result = maintenance.run(more=True)
        assert result.models_added == 1 and result.rotated_logs == 1
        assert result.deleted_ledger == result.expired_inbox == 0
        assert count(writer, "ledger") == 2 and count(writer, "inbox") == 1
        assert count(writer, "retention_run") == 0
        finished = maintenance.run(more=False)
        assert finished.deleted_ledger == finished.expired_inbox == 1
        assert count(writer, "identity") == 3 and count(writer, "ledger") == 1


@pytest.mark.parametrize("retention", [90, 180, 360, "forever"])
def test_cleanup_uses_saved_policy_and_leaves_wallet_and_history(
    tmp_path: Path, retention: int | str
) -> None:
    with WriteExecutor(tmp_path / "ledger.sqlite") as writer:
        writer.execute(create)
        writer.execute(vendor)
        writer.execute(recharge)
        writer.execute(consumption)
        writer.execute(seed)
        expire_pending(writer)
        assert retention in (90, 180, 360, "forever")
        policy = (
            90 if retention == 90 else 180 if retention == 180 else 360 if retention == 360 else "forever"
        )
        writer.execute(lambda w: MaintenanceRepository(w.connection).save_retention(w, policy))
        before = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
        result = AutoMaintenance(SqliteMaintenanceStore(writer), FakeClock(AT)).run(more=False, paused=True)
        expected = 0 if retention == "forever" else 1
        assert result.deleted_ledger == result.expired_inbox == expected
        assert count(writer, "ledger") == 1 and count(writer, "inbox") == 1 - expected
        assert count(writer, "usage_rollup") == 0
        assert count(writer, "usage_archive") == 0
        assert count(writer, "usage_daily") == 1
        assert count(writer, "identity") == 3 and count(writer, "price_revision") == 1
        assert writer.execute(lambda w: WalletRepository(w.connection).totals("vendor")) == before
        assert count(writer, "wallet_entry") == 2
        assert count(writer, "model_pool") == 0
        if expected:
            assert writer.execute(lambda w: MaintenanceRepository(w.connection).state("last_retention_at"))
        assert count(writer, "retention_run") == expected


def test_rotation_limits_logs_and_expires_price_receipts_with_event(tmp_path: Path) -> None:
    published: list[object] = []
    with WriteExecutor(tmp_path / "ledger.sqlite", publish=published.append) as writer:

        def logs(work: UnitOfWork) -> None:
            repo = MaintenanceRepository(work.connection)
            repo.log(work, "old", "expired", at=OLD)
            for index in range(2010):
                repo.log(work, "recent", str(index), at=AT)
            work.connection.execute(
                "INSERT INTO operation VALUES ('old','pricing.switch','{}',?)", (format_instant(OLD),)
            )

        writer.execute(logs)
        revision = writer.execute(lambda w: w.revision("pricing"))
        result = AutoMaintenance(SqliteMaintenanceStore(writer), FakeClock(AT)).run(more=True, paused=True)
        assert result.rotated_logs == 11 and count(writer, "diagnostic_log") == 2000
        assert count(writer, "operation") == 0 and published == [PricingChanged(revision + 1)]
        assert writer.execute(
            lambda w: MaintenanceRepository(w.connection).state("last_log_rotation_at")
        ) == format_instant(AT)


def test_partial_cleanup_failure_keeps_committed_batches_and_retries(tmp_path: Path) -> None:
    published: list[object] = []
    with WriteExecutor(tmp_path / "ledger.sqlite", publish=published.append) as writer:
        writer.execute(lambda w: seed(w, 501))
        writer.execute(
            lambda w: w.connection.execute(
                "CREATE TRIGGER fail BEFORE DELETE ON ledger WHEN (SELECT COUNT(*) FROM ledger)<=2 "
                "BEGIN SELECT RAISE(ABORT,'private injected detail'); END"
            )
        )
        maintenance = AutoMaintenance(SqliteMaintenanceStore(writer), FakeClock(AT))
        result = maintenance.run(more=False, paused=True)
        assert result.deleted_ledger == 500 and result.failed == ("retention",)
        assert count(writer, "ledger") == 2 and count(writer, "identity") == 502
        assert count(writer, "retention_run") == 0
        changed = [e for e in published if isinstance(e, UsageChanged)]
        assert len(changed) == 1 and changed[0].harnesses == (Harness.CODEX,)
        assert writer.execute(lambda w: MaintenanceRepository(w.connection).state("last_log_rotation_at"))
        writer.execute(lambda w: w.connection.execute("DROP TRIGGER fail"))
        retried = maintenance.run(more=False, paused=True)
        assert retried.deleted_ledger == 1 and not retried.failed
        assert count(writer, "ledger") == 1 and count(writer, "retention_run") == 1


def test_stop_after_current_cleanup_batch_preserves_remaining_rows(tmp_path: Path) -> None:
    cancelled = Event()

    def publish(event: object) -> None:
        if isinstance(event, UsageChanged):
            cancelled.set()

    with WriteExecutor(tmp_path / "ledger.sqlite", publish=publish) as writer:
        writer.execute(lambda w: seed(w, 501))
        maintenance = AutoMaintenance(SqliteMaintenanceStore(writer), FakeClock(AT))
        result = maintenance.run(more=False, paused=True, should_stop=cancelled.is_set)
        assert result.interrupted and result.deleted_ledger == 500 and not result.failed
        assert count(writer, "ledger") == 2 and count(writer, "retention_run") == 0
        assert maintenance.run(more=False, paused=True).deleted_ledger == 1


def test_cancelled_queued_maintenance_writes_no_new_batch(tmp_path: Path) -> None:
    entered, queued, release, cancelled = Event(), Event(), Event(), Event()

    class QueuedStore(SqliteMaintenanceStore):
        def execute[T](self, function: Callable[[WorkPort], T]) -> T:
            queued.set()
            return super().execute(function)

    def block(work: object) -> None:
        entered.set()
        assert release.wait(2)

    with WriteExecutor(tmp_path / "ledger.sqlite") as writer:
        writer.execute(seed)
        blocker = writer.submit(block)
        assert entered.wait(2)
        result: Future[MaintenanceResult] = Future()

        def maintain() -> None:
            try:
                result.set_result(
                    AutoMaintenance(QueuedStore(writer), FakeClock(AT)).run(
                        more=False, paused=True, should_stop=cancelled.is_set
                    )
                )
            except Exception as error:
                result.set_exception(error)

        thread = Thread(target=maintain)
        thread.start()
        try:
            assert queued.wait(2)
            cancelled.set()
            release.set()
            blocker.result(2)
            assert result.result(2).interrupted
            assert count(writer, "ledger") == 2
            assert count(writer, "retention_run") == 0
            assert (
                writer.execute(lambda w: MaintenanceRepository(w.connection).state("last_log_rotation_at"))
                is None
            )
        finally:
            release.set()
            thread.join(2)


def test_setting_change_between_cleanup_batches_stops_old_plan(tmp_path: Path) -> None:
    class ChangingStore(SqliteMaintenanceStore):
        def delete_ledger(self, work: WorkPort, plan: RetentionPlan) -> RetentionBatch:
            result = super().delete_ledger(work, plan)
            assert isinstance(work, UnitOfWork)
            if result.count == 500:
                MaintenanceRepository(work.connection).save_retention(work, "forever")
            return result

    with WriteExecutor(tmp_path / "ledger.sqlite") as writer:
        writer.execute(lambda w: seed(w, 501))
        maintenance = AutoMaintenance(ChangingStore(writer), FakeClock(AT))
        result = maintenance.run(more=False, paused=True)
        assert result.deleted_ledger == 500 and result.failed == ("retention",)
        assert count(writer, "ledger") == 2
        assert maintenance.run(more=False, paused=True).deleted_ledger == 0
        assert count(writer, "ledger") == 1
        assert count(writer, "usage_rollup") == 0
        assert count(writer, "usage_archive") == 0
        assert count(writer, "usage_daily") == 1


def test_paused_scheduler_rotates_and_cleans_without_scanning(tmp_path: Path) -> None:
    path, home = tmp_path / "ledger.sqlite", tmp_path / "home"
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        writer.execute(seed)
        expire_pending(writer)
        writer.execute(lambda w: MaintenanceRepository(w.connection).log(w, "old", "expired", at=OLD))
        runtime = SqliteSchedulerStore(writer, pool)
        runtime.set_paused(True)
        clock = FakeMonotonic(AT)
        wait = FakeWait(clock)
        scheduler = Scheduler(
            scanner(writer, pool, home, clock),
            runtime,
            clock,
            wait=wait,
            maintenance=AutoMaintenance(SqliteMaintenanceStore(writer), clock),
        )
        scheduler.start()
        try:
            wait.parked()
            assert scheduler.status().state == "paused"
            assert count(writer, "ledger") == 1 and count(writer, "inbox") == 0
            assert count(writer, "identity") == 3
            assert not any(s.last_scan_at is not None for s in runtime.sources())
            assert count(writer, "model_pool") == 0
            expired = Instant(AT.value - timedelta(days=400))
            collect(writer, (usage(1001, at=expired),))
            writer.execute(lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=AT))
            wait.advance(29999)
            assert count(writer, "ledger") == 2
            wait.advance(1)
            assert count(writer, "ledger") == 1 and count(writer, "retention_run") == 2
            with pytest.raises(DomainError) as failure:
                scheduler.scan_now().result(2)
            assert failure.value.code == "COLLECTION_PAUSED"
        finally:
            scheduler.close(2)


def test_scan_completion_is_published_after_model_sync_and_log_rotation(tmp_path: Path) -> None:
    path, home = tmp_path / "ledger.sqlite", tmp_path / "home"
    (sessions(home) / "one.jsonl").write_bytes(headers() + request(1, AT))
    snapshots: list[tuple[int, str | None]] = []

    def publish(event: object) -> None:
        if isinstance(event, CollectionCompleted):
            with sqlite3.connect(path) as db:
                row = db.execute(
                    "SELECT value FROM runtime_state WHERE key='last_log_rotation_at'"
                ).fetchone()
                snapshots.append(
                    (db.execute("SELECT COUNT(*) FROM model_pool").fetchone()[0], row[0] if row else None)
                )

    with WriteExecutor(path, publish=publish) as writer, ReadPool(path) as pool:
        writer.execute(lambda w: seed_prices(w, AT))
        clock = FakeClock(AT)
        scheduler = Scheduler(
            scanner(writer, pool, home, clock),
            SqliteSchedulerStore(writer, pool),
            clock,
            maintenance=AutoMaintenance(SqliteMaintenanceStore(writer), clock),
        )
        future = scheduler.scan_now()
        scheduler.start()
        try:
            assert future.result(2).result.billing.batch.billed == 1
            assert snapshots == [(1, format_instant(AT))]
        finally:
            scheduler.close(2)
