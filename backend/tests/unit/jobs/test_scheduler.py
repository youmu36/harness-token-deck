"""Advance scheduling time explicitly; real condition barriers never sleep for deadlines."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from threading import Condition, Event

import pytest
from htu.application.collection.billing import BillingResult
from htu.application.collection.scan import ScanResult
from htu.application.ports import BillBatchResult, ScanTrigger, SourceRecord
from htu.domain.common.errors import DomainError
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import Instant
from htu.jobs.scheduler import MaintenanceHook, Scheduler


class FakeMonotonic(FakeClock):
    """The coordinator's wall and monotonic clocks are advanced under its wait condition."""


class FakeWait:
    def __init__(self, clock: FakeMonotonic) -> None:
        self.clock = clock
        self.changed = Condition()
        self.condition: Condition | None = None
        self.sequence = 0

    def __call__(self, condition: Condition, timeout: float) -> None:
        assert 0 <= timeout <= 15
        with self.changed:
            self.condition = condition
            self.sequence += 1
            self.changed.notify_all()
        condition.wait()

    def parked(self, after: int = 0) -> None:
        with self.changed:
            assert self.changed.wait_for(lambda: self.sequence > after, timeout=2)

    def advance(self, milliseconds: int = 0, *, wall_seconds: int = 0) -> None:
        sequence = self.sequence
        assert self.condition is not None
        with self.condition:
            self.clock.advance(milliseconds)
            if wall_seconds:
                self.clock.set_wall(Instant(self.clock.now_utc().value + timedelta(seconds=wall_seconds)))
            self.condition.notify_all()
        self.parked(sequence)


@dataclass
class MemoryStore:
    is_paused: bool = False
    fail_pause: bool = False
    triggers: list[ScanTrigger] = field(default_factory=list[ScanTrigger])
    watcher_states: list[bool] = field(default_factory=list[bool])

    def paused(self) -> bool:
        return self.is_paused

    def set_paused(self, paused: bool) -> None:
        if self.fail_pause:
            raise ValueError("injected pause persistence failure")
        self.is_paused = paused

    def sources(self) -> tuple[SourceRecord, ...]:
        return ()

    def watcher_status(self, available: bool) -> None:
        self.watcher_states.append(available)

    def completed(self, trigger: ScanTrigger, *, watcher_available: bool) -> int:
        self.triggers.append(trigger)
        self.watcher_status(watcher_available)
        return len(self.triggers)


class ControlledScan:
    def __init__(self, clock: FakeMonotonic) -> None:
        self.clock = clock
        self.calls = 0
        self.gates: dict[tuple[int, str], Event] = {}
        self.entered: dict[tuple[int, str], Event] = {}
        self.more: set[int] = set()
        self.errors: set[int] = set()
        self.durations: dict[int, int] = {}

    def block(self, number: int, phase: str = "read") -> Event:
        gate = self.gates[number, phase] = Event()
        self.entered[number, phase] = Event()
        return gate

    def wait_phase(self, number: int, phase: str = "read") -> None:
        assert self.entered[number, phase].wait(2)

    def run(self, *, should_stop: Callable[[], bool]) -> ScanResult:
        self.calls += 1
        number = self.calls
        for phase in ("read", "bill"):
            if (number, phase) in self.gates:
                self.entered[number, phase].set()
                assert self.gates[number, phase].wait(2)
            if should_stop():
                return ScanResult((), BillingResult(BillBatchResult(0, 0, 0, True, ())), True)
        if number in self.errors:
            raise ValueError("injected billing failure")
        self.clock.advance(self.durations.get(number, 0))
        return ScanResult((), BillingResult(BillBatchResult(0, 0, 0, number in self.more, ())))


class Rig:
    def __init__(self, *, paused: bool = False, maintenance: MaintenanceHook | None = None) -> None:
        self.clock = FakeMonotonic()
        self.wait = FakeWait(self.clock)
        self.store = MemoryStore(paused)
        self.scan = ControlledScan(self.clock)
        self.scheduler = Scheduler(self.scan, self.store, self.clock, wait=self.wait, maintenance=maintenance)
        self.scheduler.start()
        self.wait.parked()

    def close(self) -> None:
        for gate in self.scan.gates.values():
            gate.set()
        self.scheduler.close(2)


@pytest.fixture
def make_rig() -> Iterator[Callable[..., Rig]]:
    rigs: list[Rig] = []

    def make(*, paused: bool = False, maintenance: MaintenanceHook | None = None) -> Rig:
        rig = Rig(paused=paused, maintenance=maintenance)
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        rig.close()


def code(future: object, expected: str) -> None:
    from concurrent.futures import Future

    assert isinstance(future, Future)
    with pytest.raises(DomainError) as failure:
        future.result(timeout=2)
    assert failure.value.code == expected


def test_periodic_runs_thirty_seconds_after_completion(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    assert rig.scan.calls == 1 and rig.store.triggers == ["startup"]
    rig.scan.durations[2] = 5000
    rig.wait.advance(29999)
    assert rig.scan.calls == 1
    rig.wait.advance(1)
    assert rig.scan.calls == 2
    assert rig.scheduler.status().next_periodic_at.value.second == 5  # 00:01:05
    rig.wait.advance(29999)
    assert rig.scan.calls == 2
    rig.wait.advance(1)
    assert rig.store.triggers == ["startup", "periodic", "periodic"]


def test_notifications_merge_and_never_postpone_periodic(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    original = rig.scheduler.status().next_periodic_at
    rig.wait.advance(5000)
    rig.scheduler.notify()
    rig.wait.advance(1000)
    rig.scheduler.notify()
    rig.wait.advance(999)
    assert rig.scan.calls == 1
    rig.wait.advance(1)
    assert rig.store.triggers[-1] == "notification"
    assert rig.scheduler.status().next_periodic_at == original
    rig.wait.advance(22000)
    rig.scheduler.notify()  # Debounce would end at 31; periodic is due at 30.
    rig.wait.advance(1000)
    assert rig.store.triggers == ["startup", "notification", "periodic"]
    rig.wait.advance(1000)
    assert rig.scan.calls == 3


def test_manual_future_waits_for_reading_and_billing(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    read, bill = rig.scan.block(2), rig.scan.block(2, "bill")
    future = rig.scheduler.scan_now()
    rig.scan.wait_phase(2)
    assert not future.done()
    read.set()
    rig.scan.wait_phase(2, "bill")
    assert not future.done()
    sequence = rig.wait.sequence
    bill.set()
    completion = future.result(2)
    rig.wait.parked(sequence)
    assert completion.trigger == "manual" and completion.revision == 2


def test_manual_during_scan_attaches_to_next_round(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    current_gate, next_gate = rig.scan.block(2), rig.scan.block(3, "bill")
    first = rig.scheduler.scan_now()
    rig.scan.wait_phase(2)
    second, third = rig.scheduler.scan_now(), rig.scheduler.scan_now()
    current_gate.set()
    assert first.result(2).revision == 2
    rig.scan.wait_phase(3, "bill")
    assert not second.done() and not third.done()
    next_gate.set()
    assert second.result(2) == third.result(2)
    assert second.result().revision == 3 and rig.scan.calls == 3


def test_manual_bypasses_debounce_and_resets_periodic(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.wait.advance(1000)
    rig.scheduler.notify()
    sequence = rig.wait.sequence
    result = rig.scheduler.scan_now().result(2)
    rig.wait.parked(sequence)
    assert result.completed_at.value.second == 1 and result.next_periodic_at.value.second == 31
    rig.wait.advance(2000)
    assert rig.scan.calls == 2


def test_notification_scan_crossing_deadline_does_not_reset_periodic(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.scan.durations[2] = 5000
    rig.wait.advance(25000)
    rig.scheduler.notify()
    rig.wait.advance(2000)
    assert rig.store.triggers == ["startup", "notification", "periodic"]
    assert rig.clock.monotonic() == 32
    assert rig.scheduler.status().next_periodic_at.value.minute == 1
    assert rig.scheduler.status().next_periodic_at.value.second == 2


def test_more_continues_after_100ms_and_pause_is_responsive(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.scan.more.update((2, 3))
    sequence = rig.wait.sequence
    rig.scheduler.scan_now().result(2)
    rig.wait.parked(sequence)
    rig.wait.advance(99)
    assert rig.scan.calls == 2
    rig.wait.advance(1)
    assert rig.scan.calls == 3 and rig.store.triggers[-1] == "baseline"
    rig.scheduler.set_paused(True)
    rig.wait.advance(1000)
    assert rig.scan.calls == 3 and rig.scheduler.status().state == "paused"
    code(rig.scheduler.scan_now(), "COLLECTION_PAUSED")
    rig.scheduler.notify()
    rig.wait.advance(30000)
    assert rig.scan.calls == 3
    sequence = rig.wait.sequence
    rig.scheduler.set_paused(False)
    rig.wait.parked(sequence)
    assert rig.scan.calls == 4 and rig.store.triggers[-1] == "resume"


def test_pause_finishes_current_batch_and_rejects_next_manual(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    gate = rig.scan.block(2, "bill")
    current = rig.scheduler.scan_now()
    rig.scan.wait_phase(2, "bill")
    pending = rig.scheduler.scan_now()
    rig.scheduler.set_paused(True)
    assert rig.store.is_paused
    code(pending, "COLLECTION_PAUSED")
    assert not current.done()
    sequence = rig.wait.sequence
    gate.set()
    code(current, "COLLECTION_PAUSED")
    rig.wait.parked(sequence)
    assert rig.scan.calls == 2 and rig.store.triggers == ["startup"]


def test_persisted_pause_prevents_startup_scan(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(paused=True)
    assert rig.scan.calls == 0
    rig.wait.advance(90000)
    assert rig.scan.calls == 0
    code(rig.scheduler.scan_now(), "COLLECTION_PAUSED")
    sequence = rig.wait.sequence
    rig.scheduler.set_paused(False)
    rig.wait.parked(sequence)
    assert rig.scan.calls == 1


@pytest.mark.parametrize("elapsed,wall", [(120000, 0), (0, 120), (0, -120), (120000, -120)])
def test_sleep_clock_jumps_cause_one_scan_without_catchup(
    make_rig: Callable[..., Rig], elapsed: int, wall: int
) -> None:
    rig = make_rig()
    rig.wait.advance(elapsed, wall_seconds=wall)
    assert rig.scan.calls == 2 and rig.store.triggers[-1] == "resume"
    rig.wait.advance()
    assert rig.scan.calls == 2
    if elapsed:
        rig.wait.advance(29999)
        assert rig.scan.calls == 2
        rig.wait.advance(1)
        assert rig.scan.calls == 3


def test_close_cancels_queued_request_and_reports_blocked_transaction(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    gate = rig.scan.block(2)
    current = rig.scheduler.scan_now()
    rig.scan.wait_phase(2)
    pending = rig.scheduler.scan_now()
    with pytest.raises(DomainError) as failure:
        rig.scheduler.close(0)
    assert failure.value.code == "COLLECTION_CLOSE_TIMEOUT"
    code(pending, "COLLECTION_STOPPED")
    gate.set()
    rig.scheduler.close(2)
    code(current, "COLLECTION_STOPPED")
    assert rig.scan.calls == 2
    code(rig.scheduler.scan_now(), "COLLECTION_STOPPED")


def test_sleep_during_scan_is_detected_after_current_round(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    gate = rig.scan.block(2)
    future = rig.scheduler.scan_now()
    rig.scan.wait_phase(2)
    rig.clock.advance(120000)
    sequence = rig.wait.sequence
    gate.set()
    future.result(2)
    rig.wait.parked(sequence)
    assert rig.store.triggers == ["startup", "manual", "resume"]
    rig.wait.advance()
    assert rig.scan.calls == 3


def test_round_failure_is_retryable_and_future_callback_can_reenter(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.scan.errors.add(2)
    future = rig.scheduler.scan_now()
    with pytest.raises(ValueError, match="injected billing failure"):
        future.result(2)
    next_future = rig.scheduler.scan_now()
    states: list[str] = []
    next_future.add_done_callback(lambda _: states.append(rig.scheduler.status().state))
    assert next_future.result(2).revision == 2
    rig.wait.parked(1)
    assert states == ["idle"]


def test_failed_pause_persistence_restores_control_state(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.store.fail_pause = True
    with pytest.raises(ValueError, match="persistence failure"):
        rig.scheduler.set_paused(True)
    assert not rig.store.is_paused
    assert rig.scheduler.scan_now().result(2).trigger == "manual"


def test_idle_close_is_idempotent_and_within_ten_seconds(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.scheduler.close(2)
    rig.scheduler.close(2)
    assert rig.scheduler.status().state == "stopping"
    with pytest.raises(DomainError, match="已停止"):
        rig.scheduler.start()


class RecordingMaintenance:
    def __init__(self) -> None:
        self.calls: list[tuple[bool, bool]] = []
        self.entered, self.release = Event(), Event()
        self.block = False
        self.fail = False
        self.stopped = False
        self.stop_checks: list[bool] = []

    def run(self, *, more: bool, paused: bool, should_stop: Callable[[], bool]) -> None:
        self.calls.append((more, paused))
        if self.block:
            self.entered.set()
            assert self.release.wait(2)
            self.stopped = should_stop()
            self.stop_checks.append(self.stopped)
        if self.fail:
            raise ValueError("injected maintenance failure")


def test_maintenance_waits_before_completed_event_and_manual_future(make_rig: Callable[..., Rig]) -> None:
    hook = RecordingMaintenance()
    rig = make_rig(maintenance=hook)
    assert hook.calls == [(False, False)]
    hook.block = True
    future = rig.scheduler.scan_now()
    try:
        assert hook.entered.wait(2)
        assert not future.done() and rig.store.triggers == ["startup"]
        hook.release.set()
        assert future.result(2).revision == 2
        assert hook.calls == [(False, False), (False, False)]
    finally:
        hook.release.set()


def test_more_is_forwarded_and_paused_maintenance_runs_every_thirty_seconds(
    make_rig: Callable[..., Rig],
) -> None:
    hook = RecordingMaintenance()
    rig = make_rig(paused=True, maintenance=hook)
    assert rig.scan.calls == 0 and hook.calls == [(False, True)]
    rig.wait.advance(29999)
    assert len(hook.calls) == 1
    rig.wait.advance(1)
    assert hook.calls == [(False, True), (False, True)]
    code(rig.scheduler.scan_now(), "COLLECTION_PAUSED")
    rig.scan.more.add(1)
    sequence = rig.wait.sequence
    rig.scheduler.set_paused(False)
    rig.wait.parked(sequence)
    assert hook.calls[-1] == (True, False)
    rig.scheduler.set_paused(True)
    rig.wait.advance(30000)
    assert hook.calls[-1] == (False, True) and rig.scan.calls == 1


def test_maintenance_failure_does_not_fail_committed_scan_or_paused_coordinator(
    make_rig: Callable[..., Rig],
) -> None:
    hook = RecordingMaintenance()
    hook.fail = True
    rig = make_rig(maintenance=hook)
    assert rig.scheduler.scan_now().result(2).trigger == "manual"
    rig.scheduler.set_paused(True)
    rig.wait.advance(30000)
    assert rig.scheduler.status().state == "paused" and hook.calls[-1] == (False, True)


def test_close_interrupts_maintenance_without_starting_new_round(make_rig: Callable[..., Rig]) -> None:
    hook = RecordingMaintenance()
    rig = make_rig(maintenance=hook)
    hook.block = True
    future = rig.scheduler.scan_now()
    try:
        assert hook.entered.wait(2)
        with pytest.raises(DomainError) as failure:
            rig.scheduler.close(0)
        assert failure.value.code == "COLLECTION_CLOSE_TIMEOUT"
        hook.release.set()
        rig.scheduler.close(2)
        code(future, "COLLECTION_STOPPED")
        assert hook.stopped and rig.store.triggers == ["startup"]
    finally:
        hook.release.set()


def test_resuming_collection_cancels_paused_maintenance_at_batch_boundary(
    make_rig: Callable[..., Rig],
) -> None:
    hook = RecordingMaintenance()
    rig = make_rig(paused=True, maintenance=hook)
    hook.block = True
    assert rig.wait.condition is not None
    with rig.wait.condition:
        rig.clock.advance(30000)
        rig.wait.condition.notify_all()
    try:
        assert hook.entered.wait(2)
        rig.scheduler.set_paused(False)
        future = rig.scheduler.scan_now()
        hook.release.set()
        assert future.result(2).trigger == "manual"
        assert hook.stop_checks == [True, False]
        assert rig.scan.calls == 1
    finally:
        hook.release.set()
