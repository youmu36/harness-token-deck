"""Composition root shared by the headless bridge, smoke mode and desktop host."""

import logging
import os
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import cast

from htd.adapters.collectors.claude_reader import ClaudeReader
from htd.adapters.collectors.codex_logs import CodexLogs
from htd.adapters.collectors.codex_reader import CodexReader
from htd.adapters.collectors.discovery import Discovery
from htd.adapters.collectors.dsh_reader import DshReader
from htd.adapters.collectors.provider_metadata import provider_metadata
from htd.adapters.collectors.zcode_reader import ZcodeReader
from htd.adapters.credentials.winvault import LazyWinVaultCredentialStore
from htd.adapters.data_directory import DataLocation, ensure_dataset, resolve_data_directory
from htd.adapters.data_migration import DataDirectoryManager
from htd.adapters.desktop.startup import set_startup
from htd.adapters.providers.lazy import LazyNewApiProvider
from htd.adapters.sqlite.auto_maintenance_store import SqliteMaintenanceStore
from htd.adapters.sqlite.calibration_store import SqliteCalibrationStore
from htd.adapters.sqlite.collection_store import SqliteCollectionStore
from htd.adapters.sqlite.command_store import SqliteCommandStore
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.pricing_store import SqlitePricingStore
from htd.adapters.sqlite.recalc_command_store import SqliteRecalcStore
from htd.adapters.sqlite.reconciliation import SqliteReconciliationStore
from htd.adapters.sqlite.remote_store import SqliteRemoteStore
from htd.adapters.sqlite.scheduler_store import SqliteSchedulerStore
from htd.adapters.sqlite.statistics_store import SqliteStatisticsStore
from htd.adapters.sqlite.unpriced_store import SqliteUnpricedStore
from htd.adapters.sqlite.wallet_command_store import SqliteWalletCommandStore
from htd.adapters.sqlite.window_state import WindowGeometry, WindowStateStore
from htd.adapters.sqlite.writer import WriteExecutor
from htd.adapters.watch.watcher import Watcher
from htd.application.calibration import Calibrations
from htd.application.collection.billing import BillInbox
from htd.application.collection.commands import CollectionCommands
from htd.application.collection.maintenance import AutoMaintenance
from htd.application.collection.scan import ScanRound
from htd.application.data_directory import DataDirectory
from htd.application.events import InProcessEventBus
from htd.application.maintenance import Maintenance
from htd.application.maintenance.recalc import Recalculate
from htd.application.migration import Migration
from htd.application.ports import (
    PreferencesChanged,
    PricingChanged,
    SourcesChanged,
    SourceWatcher,
    UnitOfWork,
    UsageChanged,
    WalletChanged,
)
from htd.application.preferences import Preferences
from htd.application.pricing import Pricing
from htd.application.pricing.models import Models
from htd.application.pricing.scan import ModelScan
from htd.application.pricing.unpriced import Unpriced
from htd.application.reconciliation import Reconciliation
from htd.application.remote.background import BackgroundRemote
from htd.application.remote.ports import CredentialStore
from htd.application.remote.service import Remote
from htd.application.statistics import Statistics
from htd.application.tasks import Tasks
from htd.application.wallet import Wallet
from htd.application.wallet.scan import ProviderScan
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Clock, Instant
from htd.domain.remote.ports import BalanceProvider
from htd.domain.usage.model import Harness
from htd.ipc.app import SystemActions, register_app_commands
from htd.ipc.bridge import Bridge
from htd.ipc.commands import register_commands
from htd.ipc.data_directory import register_data_directory
from htd.ipc.dto.base import Revisions
from htd.ipc.dto.events import CollectionTickEvent
from htd.ipc.events import EventForwarder
from htd.ipc.maintenance import register_maintenance
from htd.ipc.management import register_management
from htd.ipc.migration import register_migration
from htd.ipc.reconciliation import register_reconciliation
from htd.ipc.remote import register_remote
from htd.ipc.router import Router
from htd.ipc.unpriced import register_unpriced
from htd.jobs.scheduler import ScanCompletion, Scheduler
from htd.jobs.tasks import TaskPool
from htd.migration.engine import Engine
from htd.version import app_version

_LOG = logging.getLogger(__name__)


class SystemClock:
    def now_utc(self) -> Instant:
        return Instant(datetime.now(UTC))

    def monotonic(self) -> float:
        return time.monotonic()


def default_data_dir() -> Path:
    return resolve_data_directory().root


class Application:
    def __init__(
        self,
        data_dir: Path | None = None,
        *,
        data_location: DataLocation | None = None,
        clock: Clock | None = None,
        discovery: Discovery | None = None,
        watcher: SourceWatcher | None = None,
        start_scheduler: bool = True,
        cache_capacity: int = 8,
        legacy_path: Path | None = None,
        credentials: CredentialStore | None = None,
        provider_factory: Callable[[], BalanceProvider] | None = None,
    ) -> None:
        self.data_location = data_location or resolve_data_directory(data_dir)
        self.data_dir = self.data_location.root
        self.dataset_id = ensure_dataset(self.data_location)
        self.directory_changed = False
        self.clock = clock or SystemClock()
        self.bus = InProcessEventBus()
        self._resources = ExitStack()
        self._close_lock = threading.Lock()
        self._closed = False
        self._closing = False
        self._migration_events: tuple[object, ...] = ()
        try:
            self.writer = WriteExecutor(self.data_dir / "htd.sqlite3", publish=self.bus.publish)
            self._resources.callback(self.writer.close)
            self.readers = ReadPool(self.writer.path)
            self._resources.callback(self.readers.close)
            self.window_state = WindowStateStore(self.writer, self.readers)
            self.store = SqliteCommandStore(self.writer, self.readers)
            locator = discovery or Discovery()
            self.discovery = locator

            def initialize(work: UnitOfWork) -> None:
                before = work.revision("sources")
                for harness in Harness:
                    snapshot = self.store.sources(work)
                    existing = next(
                        (source for source in snapshot.sources if source.harness == harness), None
                    )
                    if existing is not None and not existing.scan_enabled:
                        continue
                    root = locator.default_path(harness)
                    available = locator.available(harness, root)
                    snapshot = self.store.sources(work)
                    existing = next(
                        (source for source in snapshot.sources if source.harness == harness), None
                    )
                    if existing is None or (available and not locator.available(harness, existing.path)):
                        self.store.set_path(work, harness, root, available=available)
                if before != work.revision("sources"):
                    work.emit(SourcesChanged(work.revision("sources")))

            self.store.execute(initialize)
            collection_store = SqliteCollectionStore(self.writer, self.readers)
            codex_logs = CodexLogs()
            self.codex_logs = codex_logs
            self.scan = ScanRound(
                collection_store,
                locator,
                {
                    Harness.CLAUDE: ClaudeReader(),
                    Harness.CODEX: CodexReader(),
                    Harness.ZCODE: ZcodeReader(),
                    Harness.DSH: DshReader(),
                },
                BillInbox(collection_store, self.clock),
                self.clock,
                unmetered=codex_logs,
                titles=codex_logs,
                sources_initialized=True,
            )

            def completed(completion: ScanCompletion) -> None:
                self.events.collection_completed(
                    CollectionTickEvent.model_validate(
                        self.collection.tick(
                            completion.completed_at,
                            completion.next_periodic_at,
                            completion.trigger,
                        )
                    )
                )

            def make_scheduler() -> Scheduler:
                return Scheduler(
                    self.scan,
                    SqliteSchedulerStore(self.writer, self.readers),
                    self.clock,
                    watcher=watcher if watcher is not None else Watcher(on_change=locator.invalidate),
                    maintenance=AutoMaintenance(SqliteMaintenanceStore(self.writer), self.clock),
                    on_completed=completed,
                    before_scan=locator.prepare,
                    drain_inbox=lambda stop: self.scan.billing.run_batch(should_stop=stop),
                    on_maintenance=self.expire_idle_caches,
                )

            self._make_scheduler = make_scheduler
            self._start_scheduler = start_scheduler
            self.scheduler = make_scheduler()
            self.preferences = Preferences(self.store, self.clock, set_startup=set_startup)
            self.collection = CollectionCommands(
                self.store,
                locator,
                self.scheduler,
                self.clock,
                # Resolve the current coordinator after a directory/import restart.
                on_sources_changed=lambda: self.scheduler.refresh_sources(),  # noqa: PLW0108
            )
            self.statistics = Statistics(
                SqliteStatisticsStore(self.readers, self.writer), self.clock, capacity=cache_capacity
            )

            self.events = EventForwarder(self.bus)
            self._resources.callback(self.events.close)
            self.task_pool = TaskPool(self.clock, self.bus)
            self._resources.callback(self.task_pool.close)
            pricing_store = SqlitePricingStore(self.writer, self.readers)
            self.pricing = Pricing(pricing_store, self.clock)
            self.models = Models(pricing_store, self.clock)
            wallet_store = SqliteWalletCommandStore(self.writer, self.readers)
            self.wallet_store = wallet_store
            self.wallet = Wallet(wallet_store, self.clock)
            self.provider_scan = ProviderScan(
                wallet_store,
                locator,
                {
                    Harness.CLAUDE: ClaudeReader(),
                    Harness.CODEX: CodexReader(),
                    Harness.ZCODE: ZcodeReader(),
                    Harness.DSH: DshReader(),
                },
                self.task_pool,
                self.clock,
                self.wallet,
                provider_metadata,
            )
            remote_store = SqliteRemoteStore(self.writer, self.readers)
            credential_store = credentials or LazyWinVaultCredentialStore()
            self.remote = BackgroundRemote(
                lambda: Remote(
                    remote_store,
                    credential_store,
                    provider_factory() if provider_factory else LazyNewApiProvider(self.clock),
                    self.clock,
                ),
                automatic=start_scheduler,
            )
            self._resources.callback(self.remote.close)
            self.model_scan = ModelScan(
                pricing_store,
                locator,
                {
                    Harness.CLAUDE: ClaudeReader(),
                    Harness.CODEX: CodexReader(),
                    Harness.ZCODE: ZcodeReader(),
                    Harness.DSH: DshReader(),
                },
                self.task_pool,
                self.clock,
            )
            self.tasks = Tasks(self.task_pool, self.store, self.clock)
            maintenance_store = SqliteRecalcStore(self.writer, self.readers)
            self.maintenance = Maintenance(maintenance_store, self.task_pool, self.clock, app_version())
            self.recalc = Recalculate(maintenance_store, self.clock)
            source = (
                legacy_path
                or Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
                / "com.harness-token-usage.desktop/ledger.sqlite3"
            )
            self.migration = Migration(
                Engine(self.data_dir, source, self.store, self.clock, self._install_migration),
                self.store,
                self.task_pool,
                self.clock,
            )
            self.router = Router()
            self.system_actions = SystemActions()
            self.directory_manager = DataDirectoryManager(
                self.data_location,
                quiesce=self._quiesce_directory,
                resume=self._resume_directory,
                committed=self._directory_committed,
            )
            register_data_directory(
                self.router,
                DataDirectory(self.directory_manager),
                self._restart_directory,
            )
            self._resources.callback(self.router.close)
            register_app_commands(
                self.router,
                app_version=app_version(),
                data_dir=str(self.data_dir),
                data_dir_source=self.data_location.source,
                revisions=self.revisions,
                events=self.events,
                system_actions=self.system_actions,
            )
            register_commands(self.router, self.preferences, self.collection, self.statistics)
            register_management(
                self.router,
                self.pricing,
                self.models,
                self.wallet,
                self.model_scan,
                self.tasks,
                self.provider_scan,
            )
            register_maintenance(self.router, self.maintenance, self.recalc)
            register_migration(self.router, self.migration, changed=self._publish_migration)
            register_remote(self.router, self.remote)
            self.unpriced = Unpriced(SqliteUnpricedStore(self.writer, self.readers), self.clock)
            register_unpriced(self.router, self.unpriced)
            register_reconciliation(
                self.router,
                Reconciliation(SqliteReconciliationStore(self.readers, self.writer), self.clock),
                Calibrations(SqliteCalibrationStore(self.writer, self.readers), self.clock),
            )
            self.directory_manager.activated()
            self.bridge = Bridge(self.router, self.events, self.system_actions)
            if start_scheduler:
                self.scheduler.start()
        except BaseException:
            self._resources.close()
            raise

    def revisions(self) -> Revisions:
        return cast(Revisions, self.store.revisions())

    def release_idle_memory(self) -> None:
        """Discard reconstructible snapshots, never live transactions or checkpoints."""
        readers_before = self.readers.diagnostics()
        statistics_before = self.statistics.diagnostics()
        self.statistics.invalidate()
        self.wallet_store.release_cache()
        self.discovery.release_cache()
        self.codex_logs.release_cache()
        self.readers.release_idle()
        import gc

        gc.collect()
        _LOG.info(
            "托盘资源回收: 读连接 %d -> %d，统计缓存 %d 项 / 估算 %d 字节 -> 0",
            readers_before["connections"],
            self.readers.diagnostics()["connections"],
            statistics_before["entries"],
            statistics_before["estimatedBytes"],
        )

    def expire_idle_caches(self) -> None:
        """Use the existing maintenance cadence; no timer, GC or live-reader interruption."""
        self.statistics.expire_idle_cache()
        self.wallet_store.expire_idle_cache()
        self.readers.expire_idle()

    def _install_migration(self, replace: Callable[[], None]) -> None:
        if self._closing:
            raise DomainError("APP_SHUTTING_DOWN", "应用正在关闭，请重新启动后激活")
        self.task_pool.require_idle()
        self.remote.suspend()
        self.scheduler.close()
        self.readers.close()
        self.writer.close()
        try:
            replace()
        finally:
            self.writer.reopen()
            self.readers.reopen()
            self.statistics.invalidate()
            self.remote.resume()
            self.scheduler = self._make_scheduler()
            self.collection.scheduler = self.scheduler
            if self._start_scheduler and not self._closing:
                self.scheduler.start()
        revisions = self.revisions()
        self._migration_events = (
            PreferencesChanged(revisions["preferences"]),
            PricingChanged(revisions["pricing"]),
            SourcesChanged(revisions["sources"]),
            UsageChanged(tuple(Harness), revisions["usage"]),
            WalletChanged((), revisions["wallet"]),
        )

    def _publish_migration(self) -> None:
        events, self._migration_events = self._migration_events, ()
        for event in events:
            self.bus.publish(event)

    def _quiesce_directory(self) -> None:
        self.task_pool.require_idle()
        self.remote.suspend()
        self.scheduler.close()
        self.readers.close()
        self.writer.close()

    def _resume_directory(self) -> None:
        self.writer.reopen()
        self.readers.reopen()
        self.remote.resume()
        self.scheduler = self._make_scheduler()
        self.collection.scheduler = self.scheduler
        if self._start_scheduler and not self._closing:
            self.scheduler.start()

    def _directory_committed(self) -> None:
        self.directory_changed = True
        self.router.park_for_restart()

    def _restart_directory(self) -> None:
        if self.directory_changed and self.system_actions.restart is not None:
            self.system_actions.restart()

    def close(self, *, window_geometry: WindowGeometry | None = None) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closing = True
            # Keep resources alive if a transaction exceeds the coordinator's deadline.
            self.scheduler.close()
            self.remote.close()
            self.router.close()
            self.task_pool.close()
            if window_geometry is not None and not self.directory_changed:
                try:
                    self.window_state.save(window_geometry)
                except Exception:
                    _LOG.exception("保存窗口尺寸与位置失败")
            self.writer.close()
            self._resources.close()
            self._closed = True

    def __enter__(self) -> "Application":
        return self

    def __exit__(
        self, kind: type[BaseException] | None, value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()
