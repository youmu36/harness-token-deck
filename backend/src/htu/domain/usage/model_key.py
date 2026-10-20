"""Shared model keys for pricing, model pools, and statistics."""

from htu.domain.common.errors import MODEL_NAME_INVALID, DomainError


def model_key(name: str) -> str:
    if type(name) is not str:
        raise DomainError(MODEL_NAME_INVALID, "模型名必须为字符串", "model_id")
    key = "".join(char for char in name if not char.isspace() and char != "\ufeff").lower()
    if not key:
        raise DomainError(MODEL_NAME_INVALID, "模型名去除空白后不能为空", "model_id")
    return key
