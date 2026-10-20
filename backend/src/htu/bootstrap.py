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

from htu.adapters.collectors.claude_reader import ClaudeReader
from htu.adapters.collectors.codex_logs import CodexLogs
from htu.adapters.collectors.codex_reader import CodexReader
from htu.adapters.collectors.discovery import Discovery
from htu.adapters.collectors.dsh_reader import DshReader
from htu.adapters.collectors.provider_metadata import provider_metadata
from htu.adapters.collectors.zcode_reader import ZcodeReader
from htu.adapters.credentials.winvault import LazyWinVaultCredentialStore
from htu.adapters.desktop.startup import set_startup
from htu.adapters.providers.newapi import NewApiProvider
from htu.adapters.sqlite.auto_maintenance_store import SqliteMaintenanceStore
from htu.adapters.sqlite.collection_store import SqliteCollectionStore
from htu.adapters.sqlite.command_store import SqliteCommandStore
from htu.adapters.sqlite.connection import ReadPool
from htu.adapters.sqlite.pricing_store import SqlitePricingStore
from htu.adapters.sqlite.recalc_command_store import SqliteRecalcStore
from htu.adapters.sqlite.reconciliation import SqliteReconciliationStore
from htu.adapters.sqlite.remote_store import SqliteRemoteStore
from htu.adapters.sqlite.scheduler_store import SqliteSchedulerStore
from htu.adapters.sqlite.statistics_store import SqliteStatisticsStore
from htu.adapters.sqlite.unpriced_store import SqliteUnpricedStore
from htu.adapters.sqlite.wallet_command_store import SqliteWalletCommandStore
from htu.adapters.sqlite.window_state import WindowGeometry, WindowStateStore
from htu.adapters.sqlite.writer import WriteExecutor
from htu.adapters.watch.watcher import Watcher
from htu.application.collection.billing import BillInbox
from htu.application.collection.commands import CollectionCommands
from htu.application.collection.maintenance import AutoMaintenance
from htu.application.collection.scan import ScanRound
from htu.application.events import InProcessEventBus
from htu.application.maintenance import Maintenance
from htu.application.maintenance.recalc import Recalculate
from htu.application.migration import Migration
from htu.application.ports import (
    PreferencesChanged,
    PricingChanged,
    SourcesChanged,
    SourceWatcher,
    UnitOfWork,
    UsageChanged,
    WalletChanged,
)
from htu.application.preferences import Preferences
from htu.application.pricing import Pricing
from htu.application.pricing.models import Models
from htu.application.pricing.scan import ModelScan
from htu.application.pricing.unpriced import Unpriced
from htu.application.reconciliation import Reconciliation
from htu.application.remote.background import BackgroundRemote
from htu.application.remote.ports import CredentialStore
from htu.application.remote.service import Remote
from htu.application.statistics import Statistics
from htu.application.tasks import Tasks
from htu.application.wallet import Wallet
from htu.application.wallet.scan import ProviderScan
from htu.domain.common.errors import DomainError
from htu.domain.common.time import Clock, Instant
from htu.domain.remote.ports import BalanceProvider
from htu.domain.usage.model import Harness
from htu.ipc.app import SystemActions, register_app_commands
from htu.ipc.bridge import Bridge
from htu.ipc.commands import register_commands
from htu.ipc.dto.base import Revisions
from htu.ipc.dto.events import CollectionTickEvent
from htu.ipc.events import EventForwarder
from htu.ipc.maintenance import register_maintenance
from htu.ipc.management import register_management
from htu.ipc.migration import register_migration
from htu.ipc.reconciliation import register_reconciliation
from htu.ipc.remote import register_remote
from htu.ipc.router import Router
from htu.ipc.unpriced import register_unpriced
from htu.jobs.scheduler import ScanCompletion, Scheduler
from htu.jobs.tasks import TaskPool
from htu.migration.engine import Engine
from htu.version import app_version

_LOG = logging.getLogger(__name__)


class SystemClock:
    def now_utc(self) -> Instant:
        return Instant(datetime.now(UTC))

    def monotonic(self) -> float:
        return time.monotonic()


def default_data_dir() -> Path:
    configured = os.environ.get("HTU_DATA_DIR")
    if configured:
        return Path(configured).resolve()
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) if local else Path.home() / "AppData/Local") / "HarnessTokenUsage"


class Application:
    def __init__(
        self,
        data_dir: Path | None = None,
        *,
        clock: Clock | None = None,
        discovery: Discovery | None = None,
        watcher: SourceWatcher | None = None,
        start_scheduler: bool = True,
        cache_capacity: int = 8,
        legacy_path: Path | None = None,
        credentials: CredentialStore | None = None,
        provider_factory: Callable[[], BalanceProvider] | None = None,
    ) -> None:
        self.data_dir = (data_dir or default_data_dir()).resolve()
        self.clock = clock or SystemClock()
        self.bus = InProcessEventBus()
        self._resources = ExitStack()
        self._close_lock = threading.Lock()
        self._closed = False
        self._closing = False
        self._migration_events: tuple[object, ...] = ()
        try:
            self.writer = WriteExecutor(self.data_dir / "htu.sqlite3", publish=self.bus.publish)
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
                )

            self._make_scheduler = make_scheduler
            self._start_scheduler = start_scheduler
            self.scheduler = make_scheduler()
            self.preferences = Preferences(self.store, self.clock, set_startup=set_startup)
            self.collection = CollectionCommands(self.store, locator, self.scheduler, self.clock)
            self.statistics = Statistics(
                SqliteStatisticsStore(self.readers), self.clock, capacity=cache_capacity
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
                    provider_factory() if provider_factory else NewApiProvider(self.clock),
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
            self._resources.callback(self.router.close)
            register_app_commands(
                self.router,
                app_version=app_version(),
                data_dir=str(self.data_dir),
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
                self.router, Reconciliation(SqliteReconciliationStore(self.readers), self.clock)
            )
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
            if window_geometry is not None:
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
