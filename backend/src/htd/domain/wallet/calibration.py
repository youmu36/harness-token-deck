"""Explicit member scopes and target arithmetic; never infer allocations from cost proportions."""

from typing import Any

from htd.domain.common.errors import DomainError

DIMENSIONS = {
    "localBatchIds": "batch_id",
    "harnesses": "harness",
    "sessionRefs": "session_ref",
    "modelKeys": "model_key",
    "groupIds": "group_id",
    "versionIds": "version_id",
}


def nonnegative_pico(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or len(value) > 39:
        raise DomainError("CALIBRATION_AMOUNT_INVALID", "目标须为精确非负 pico 金额")
    amount = int(value)
    if amount >= 2**127:
        raise DomainError("CALIBRATION_AMOUNT_INVALID", "目标金额超出范围")
    return amount


def matches(member: Any, scope: dict[str, Any]) -> bool:
    return all(not scope.get(key) or member[field] in scope[key] for key, field in DIMENSIONS.items())


def validate_conservation(target: int, days: list[dict[str, Any]]) -> None:
    if not days or len({row["day"] for row in days}) != len(days):
        raise DomainError("CALIBRATION_DATES_REQUIRED", "请明确每个业务日的目标，日期不能重复")
    if sum(nonnegative_pico(row["targetPico"]) for row in days) != target:
        raise DomainError("CALIBRATION_ALLOCATION_INVALID", "每天目标之和须等于批次目标")
