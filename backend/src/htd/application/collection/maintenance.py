"""Run automatic maintenance after billing and during paused collection, in bounded transactions."""

import logging
from collections.abc import Callable, Generator
from contextlib import closing
from dataclasses import dataclass, field
from datetime import timedelta

from htd.application.collection.cancellation import CollectionInterrupted, check_cancelled
from htd.application.ports import (
    MaintenanceStore,
    ModelsChanged,
    PricingChanged,
    SourcesChanged,
    UnitOfWork,
    UsageChanged,
    WalletChanged,
)
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Clock, Instant
from htd.domain.pricing.model import model_name
from htd.domain.pricing.model_pool import sync_models
from htd.domain.usage.model_key import model_key

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    models_added: int
    pool_full: bool
    wallets_repaired: tuple[str, ...]
    deleted_ledger: int
    expired_inbox: int
    rotated_logs: int
    interrupted: bool
    failed: tuple[str, ...]


@dataclass
class _Progress:
    models_added: int = 0
    pool_full: bool = False
    wallets_repaired: tuple[str, ...] = ()
    deleted_ledger: int = 0
    expired_inbox: int = 0
    rotated_logs: int = 0
    interrupted: bool = False
    failed: list[str] = field(default_factory=list[str])


class AutoMaintenance:
    def __init__(self, store: MaintenanceStore, clock: Clock) -> None:
        self.store, self.clock = store, clock
        self._models_revision: tuple[int, int] | None = None
        self._compacted: tuple[int, int, Instant] | None = None

    def _execute[T](self, action: Callable[[UnitOfWork], T], should_stop: Callable[[], bool]) -> T:
        def guarded(work: UnitOfWork) -> T:
            check_cancelled(should_stop)
            return action(work)

        check_cancelled(should_stop)
        try:
            return self.store.execute(guarded)
        except BaseException:
            # A failed commit must not retain memoized work from the rolled-back transaction.
            self._models_revision = None
            self._compacted = None
            raise

    def _sync(self, work: UnitOfWork, at: Instant, should_stop: Callable[[], bool]) -> tuple[int, bool]:
        revision = (work.revision("usage"), work.revision("models"))
        if revision == self._models_revision:
            return 0, False
        old = self.store.model_pool(work)
        seen = {model_key(name) for name in old.models}
        excluded = set(old.excluded)
        full = False

        def candidates(names: Generator[str, None, None]) -> Generator[str, None, None]:
            nonlocal full
            for raw in names:
                check_cancelled(should_stop)
                try:
                    name = model_name(raw)
                except DomainError:
                    continue
                key = model_key(name)
                if key in seen or key in excluded:
                    continue
                if len(seen) == 5000:
                    full = True
                    break
                seen.add(key)
                yield name

        with closing(self.store.observed_models(work)) as names:
            new = sync_models(old, candidates(names))
        self.store.save_models(work, old, new, at=at)
        if new != old:
            work.emit(ModelsChanged(work.revision("models")))
        if full:
            self.store.log(
                work, "model_pool_full", "模型池已达 5000 个，自动同步跳过新增模型", at=at, once_per_day=True
            )
        if not full:
            self._models_revision = (work.revision("usage"), work.revision("models"))
        return len(new.models) - len(old.models), full

    def _compact(self, work: UnitOfWork, at: Instant) -> int:
        marker = (
            work.revision("usage"),
            work.revision("wallet"),
            Instant((at.value - timedelta(hours=72)).replace(second=0, microsecond=0)),
        )
        if self._compacted == marker:
            return 0
        count = self.store.compact_history(work, at=at)
        if count < 500:
            self._compacted = (work.revision("usage"), work.revision("wallet"), marker[2])
        return count

    def _wallet(self, work: UnitOfWork, at: Instant) -> tuple[str, ...]:
        repaired = self.store.validate_wallet(work, at=at)
        if repaired:
            work.emit(WalletChanged(repaired, work.revision("wallet")))
        return repaired

    def _retention(self, progress: _Progress, at: Instant, should_stop: Callable[[], bool]) -> None:
        plan = self._execute(lambda work: self.store.retention_plan(work, at=at), should_stop)
        if not plan.due or plan.cutoff is None:
            return

        def expire(work: UnitOfWork) -> int:
            count = self.store.expire_inbox(work, plan, at=at)
            if count:
                work.emit(SourcesChanged(work.revision("sources")))
            return count

        def delete(work: UnitOfWork):
            batch = self.store.delete_ledger(work, plan)
            if batch.count:
                work.emit(UsageChanged(batch.harnesses, work.revision("usage")))
                work.emit(SourcesChanged(work.revision("sources")))
            return batch

        while True:
            count = self._execute(expire, should_stop)
            progress.expired_inbox += count
            if count < 500:
                break
        while True:
            batch = self._execute(delete, should_stop)
            progress.deleted_ledger += batch.count
            if not (batch.more if batch.more is not None else batch.count >= 500):
                break
        self._execute(
            lambda work: self.store.finish_retention(
                work, plan, deleted=progress.deleted_ledger, expired=progress.expired_inbox, at=at
            ),
            should_stop,
        )

    def _rotate(self, work: UnitOfWork, at: Instant) -> int:
        before = work.revision("pricing")
        count = self.store.rotate_logs(work, at=at)
        if work.revision("pricing") != before:
            work.emit(PricingChanged(work.revision("pricing")))
        return count

    def run(
        self, *, more: bool, paused: bool = False, should_stop: Callable[[], bool] = lambda: False
    ) -> MaintenanceResult:
        at, progress = self.clock.now_utc(), _Progress()

        def models() -> None:
            progress.models_added, progress.pool_full = self._execute(
                lambda work: self._sync(work, at, should_stop), should_stop
            )

        def wallet() -> None:
            progress.wallets_repaired = self._execute(lambda work: self._wallet(work, at), should_stop)

        def rotate() -> None:
            progress.rotated_logs = self._execute(lambda work: self._rotate(work, at), should_stop)

        def compact() -> None:
            if "retention" in progress.failed:
                return
            while self._execute(lambda work: self._compact(work, at), should_stop) == 500:
                check_cancelled(should_stop)

        stages: list[tuple[str, Callable[[], None]]] = []
        if not paused:
            stages.append(("models", models))
        stages.append(("wallet", wallet))
        if not more:
            stages.append(("retention", lambda: self._retention(progress, at, should_stop)))
            stages.append(("compaction", compact))
        stages.append(("rotation", rotate))
        for stage, action in stages:
            try:
                action()
            except CollectionInterrupted:
                progress.interrupted = True
                break
            except Exception:
                progress.failed.append(stage)
                _LOG.exception("Automatic %s maintenance failed", stage)
                label = {
                    "models": "模型同步",
                    "wallet": "钱包校验",
                    "retention": "保留清理",
                    "rotation": "日志轮转",
                    "compaction": "历史汇总",
                }[stage]
                detail = f"自动{label}失败，等待下轮重试"
                if stage == "retention":
                    detail += (
                        f"。已删除账本 {progress.deleted_ledger} 条，过期入箱 {progress.expired_inbox} 条"
                    )
                try:
                    self._execute(
                        lambda work, detail=detail: self.store.log(
                            work,
                            "automatic_maintenance_failed",
                            detail,
                            at=at,
                        ),
                        should_stop,
                    )
                except CollectionInterrupted:
                    progress.interrupted = True
                    break
                except Exception:
                    _LOG.exception("Automatic maintenance diagnostic could not be persisted")
        return MaintenanceResult(
            progress.models_added,
            progress.pool_full,
            progress.wallets_repaired,
            progress.deleted_ledger,
            progress.expired_inbox,
            progress.rotated_logs,
            progress.interrupted,
            tuple(progress.failed),
        )
