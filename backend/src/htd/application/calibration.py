"""Explicit local-consumption targets. Reads never refresh remote accounts or post money."""

from typing import Protocol

from htd.application.command import execute
from htd.application.ports import CommandStore, CostChanged, UnitOfWork, WalletChanged
from htd.domain.common.time import Clock, Instant


class CalibrationStore(CommandStore, Protocol):
    def calibration_preview(self, parameters: dict[str, object], at: Instant) -> dict[str, object]: ...

    def calibration_commit(
        self, work: UnitOfWork, parameters: dict[str, object], operation: str, at: Instant
    ) -> dict[str, object]: ...

    def calibration_targets(self, parameters: dict[str, object]) -> dict[str, object]: ...


class Calibrations:
    def __init__(self, store: CalibrationStore, clock: Clock) -> None:
        self.store, self.clock = store, clock

    def preview(self, parameters: dict[str, object]) -> dict[str, object]:
        return self.store.calibration_preview(parameters, self.clock.now_utc())

    def targets(self, parameters: dict[str, object]) -> dict[str, object]:
        return self.store.calibration_targets(parameters)

    def commit(self, parameters: dict[str, object], operation: str) -> dict[str, object]:
        def save(work: UnitOfWork, at: Instant) -> dict[str, object]:
            result = self.store.calibration_commit(work, parameters, operation, at)
            work.emit(CostChanged(work.revision("cost")))
            work.emit(WalletChanged((str(parameters["vendorId"]),), work.revision("wallet")))
            return result

        return execute(
            self.store, self.clock, "reconciliation.commitCalibration", parameters, operation, save
        )
