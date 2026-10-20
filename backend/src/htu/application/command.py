"""Atomic command receipts and revision guards shared by management use cases."""

from collections.abc import Callable

from htu.application.ports import CommandStore, UnitOfWork
from htu.domain.common.errors import DomainError
from htu.domain.common.time import Clock, Instant


def expected(work: UnitOfWork, scope: str, revision: int, *, field: str = "expected_revision") -> None:
    current = work.revision(scope)
    if current != revision:
        raise DomainError(
            f"{scope.upper()}_REVISION_CONFLICT",
            "数据已更新，请刷新后重试",
            field,
            conflict_revision=current,
        )


def execute(
    store: CommandStore,
    clock: Clock,
    command: str,
    parameters: dict[str, object],
    operation_id: str,
    action: Callable[[UnitOfWork, Instant], dict[str, object]],
) -> dict[str, object]:
    def save(work: UnitOfWork) -> dict[str, object]:
        replay = store.replay(work, operation_id, command, parameters)
        if replay is not None:
            return replay
        at = clock.now_utc()
        result = action(work, at)
        store.record(work, operation_id, command, parameters, result, at=at)
        return result

    return store.execute(save)
