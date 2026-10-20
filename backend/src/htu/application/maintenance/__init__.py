"""Manual maintenance commands and resumable bounded retention cleanup."""

import logging
from datetime import timedelta
from threading import Lock
from typing import cast

from htu.application.command import execute, expected
from htu.application.ports import (
    PreferencesChanged,
    RetentionPlan,
    SourcesChanged,
    TaskContext,
    TaskControl,
    UnitOfWork,
    UsageChanged,
)
from htu.application.ports.maintenance import ManualMaintenanceStore, Retention
from htu.application.tasks import start_task
from htu.domain.common.errors import DomainError
from htu.domain.common.time import Clock, Instant, format_instant, parse_instant

_LOG = logging.getLogger(__name__)


class Maintenance:
    def __init__(
        self, store: ManualMaintenanceStore, tasks: TaskControl, clock: Clock, app_version: str
    ) -> None:
        self.store, self.tasks, self.clock, self.app_version = store, tasks, clock, app_version
        self._retention_lock = Lock()

    def status(self) -> dict[str, object]:
        return self.store.status(at=self.clock.now_utc(), app_version=self.app_version)

    def preview(self, retention: Retention) -> dict[str, object]:
        return self.store.preview_retention(retention, at=self.clock.now_utc())

    def set_retention(self, retention: Retention, revision: int, operation_id: str) -> dict[str, object]:
        parameters: dict[str, object] = {"retention": retention, "expectedRevision": revision}
        with self._retention_lock:

            def save(work: UnitOfWork, at: Instant) -> dict[str, object]:
                expected(work, "preferences", revision)
                before = work.revision("preferences")
                self.store.save_retention(work, retention)
                current = work.revision("preferences")
                if current != before:
                    work.emit(PreferencesChanged(current))
                cutoff = (
                    None
                    if retention == "forever"
                    else format_instant(Instant(at.value - timedelta(days=retention)))
                )
                return {
                    "retention": retention,
                    "revision": current,
                    "cutoff": cutoff,
                    "deletedLedger": 0,
                    "expiredInbox": 0,
                    "complete": False,
                    "errorCode": None,
                }

            result = execute(
                self.store, self.clock, "maintenance.setRetention", parameters, operation_id, save
            )
            if result["complete"]:
                return result
            cutoff = cast(str | None, result["cutoff"])
            plan = RetentionPlan(retention, parse_instant(cutoff) if cutoff else None, True)
            result = dict(result, errorCode=None)

            more = False

            def batch(work: UnitOfWork, inbox: bool) -> int:
                nonlocal more
                at = self.clock.now_utc()
                updated = dict(result)
                if inbox:
                    count = self.store.expire_inbox(work, plan, at=at)
                    updated["expiredInbox"] = cast(int, updated["expiredInbox"]) + count
                    if count:
                        work.emit(SourcesChanged(work.revision("sources")))
                else:
                    deleted = self.store.delete_ledger(work, plan)
                    count = deleted.count
                    more = deleted.more if deleted.more is not None else count >= 500
                    updated["deletedLedger"] = cast(int, updated["deletedLedger"]) + count
                    if count:
                        work.emit(UsageChanged(deleted.harnesses, work.revision("usage")))
                        work.emit(SourcesChanged(work.revision("sources")))
                self.store.update_receipt(work, operation_id, parameters, updated)
                return count

            try:
                if plan.cutoff is not None:
                    for inbox, key in ((True, "expiredInbox"), (False, "deletedLedger")):
                        while True:
                            count = self.store.execute(lambda work, inbox=inbox: batch(work, inbox))
                            result[key] = cast(int, result[key]) + count
                            if count < 500 if inbox else not more:
                                break

                def finish(work: UnitOfWork) -> None:
                    self.store.finish_retention(
                        work,
                        plan,
                        deleted=cast(int, result["deletedLedger"]),
                        expired=cast(int, result["expiredInbox"]),
                        at=self.clock.now_utc(),
                    )
                    self.store.update_receipt(work, operation_id, parameters, dict(result, complete=True))

                self.store.execute(finish)
                return dict(result, complete=True)
            except Exception as error:
                _LOG.exception("Manual retention interrupted; committed counts are retained")
                return dict(
                    result, errorCode=error.code if isinstance(error, DomainError) else "RETENTION_FAILED"
                )

    def start_file(self, command: str, operation_id: str) -> dict[str, object]:
        def action(context: TaskContext) -> dict[str, object]:
            context.progress(0, 1)
            return self.store.file_action(
                command, at=self.clock.now_utc(), check_cancelled=context.check_cancelled
            )

        return start_task(self.store, self.clock, self.tasks, command, {}, operation_id, action)
