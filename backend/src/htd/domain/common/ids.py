"""Random entity IDs and reproducible UUID v5 IDs."""

from uuid import UUID, uuid4, uuid5

from htd.domain.common.errors import ID_INVALID, DomainError
from htd.domain.common.hashing import encode_parts


def new_id() -> str:
    return str(uuid4())


def deterministic_id(namespace: UUID | str, *parts: str | int) -> str:
    try:
        identifier = namespace if isinstance(namespace, UUID) else UUID(namespace)
    except (ValueError, AttributeError, TypeError) as error:
        raise DomainError(ID_INVALID, "确定性标识需要有效的 UUID 命名空间", "namespace") from error
    return str(uuid5(identifier, encode_parts(parts)))
