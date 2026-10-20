"""Preference patch commands independent of persistence and IPC."""

from collections.abc import Callable
from copy import deepcopy
from threading import Lock
from typing import cast

from htu.application.ports import CommandStore, PreferencesChanged, PreferenceSnapshot, UnitOfWork
from htu.domain.common.errors import DomainError
from htu.domain.common.time import Clock

DEFAULTS: dict[str, object] = {
    "theme": "system",
    "timezone": "system",
    "showScanCountdown": False,
    "hiddenHarnesses": [],
    "settingsSection": "display",
    "walletDefaultScope": "local",
    "collapsed": False,
    "lastPage": "codex",
    "lastRoute": "",
    "guideSeen": False,
    "accountingTimezone": "Asia/Shanghai",
    "unpricedNoticeDismissed": "",
    "guideDismissed": False,
    "closeToTray": True,
    "startAtLogin": False,
    "filters": {},
    "historyPrompted": False,
    "migrationHintDismissed": False,
}


class Preferences:
    def __init__(
        self, store: CommandStore, clock: Clock, *, set_startup: Callable[[bool], None] = lambda enabled: None
    ) -> None:
        self.store, self.clock = store, clock
        self.set_startup = set_startup
        self._lock = Lock()

    def get(self) -> PreferenceSnapshot:
        snapshot = self.store.preferences()
        return PreferenceSnapshot(deepcopy(DEFAULTS) | snapshot.values, snapshot.revision)

    def _startup(self, enabled: bool) -> None:
        try:
            self.set_startup(enabled)
        except OSError as error:
            raise DomainError("STARTUP_SETTING_FAILED", "无法更新开机自启设置，请重试") from error

    def update(
        self, patch: dict[str, object], expected_revision: int, operation_id: str
    ) -> dict[str, object]:
        parameters: dict[str, object] = {"patch": patch, "expectedRevision": expected_revision}
        startup_before: bool | None = None

        def save(work: UnitOfWork) -> dict[str, object]:
            nonlocal startup_before
            replay = self.store.replay(work, operation_id, "preferences.update", parameters)
            if replay is not None:
                return replay
            snapshot = self.store.preferences(work)
            if snapshot.revision != expected_revision:
                raise DomainError(
                    "PREFERENCES_REVISION_CONFLICT",
                    "偏好设置已更新，请刷新后重试",
                    "expected_revision",
                    conflict_revision=snapshot.revision,
                )
            old = deepcopy(DEFAULTS) | snapshot.values
            values = old | patch
            patch_filters = patch.get("filters")
            if isinstance(patch_filters, dict):
                old_filters = old.get("filters")
                if not isinstance(old_filters, dict):
                    raise ValueError("Stored filters are corrupt")
                values["filters"] = cast(dict[str, object], old_filters) | cast(
                    dict[str, object], patch_filters
                )
            if old.get("startAtLogin") != values.get("startAtLogin"):
                startup_before = old.get("startAtLogin") is True
                self._startup(values["startAtLogin"] is True)
            if old != values:
                self.store.save_preferences(work, values)
                work.emit(PreferencesChanged(work.touch("preferences")))
            result: dict[str, object] = {"preferences": values, "revision": work.revision("preferences")}
            self.store.record(
                work, operation_id, "preferences.update", parameters, result, at=self.clock.now_utc()
            )
            return result

        with self._lock:
            try:
                return self.store.execute(save)
            except BaseException:
                if startup_before is not None:
                    self._startup(startup_before)
                raise
