"""Full credentials stay behind this port and never enter account persistence."""

import re
from dataclasses import dataclass, field
from threading import Lock
from typing import Protocol

from htd.domain.common.errors import DomainError

_PREFIX = "HarnessTokenDeck/remote/"


def credential_reference(account_id: str) -> str:
    if type(account_id) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", account_id) is None:
        raise DomainError("REMOTE_ACCOUNT_INVALID", "远程账户标识无效")
    return _PREFIX + account_id


def validate_reference(reference: str) -> None:
    if type(reference) is not str or not reference.startswith(_PREFIX):
        raise DomainError("REMOTE_ACCOUNT_INVALID", "凭据引用无效")
    suffix = reference[len(_PREFIX) :]
    account, *version = suffix.split("/")
    credential_reference(account)
    if version and (len(version) != 1 or re.fullmatch(r"v_[0-9a-f]{32}", version[0]) is None):
        raise DomainError("REMOTE_ACCOUNT_INVALID", "凭据版本引用无效")


def validate_credential(credential: str) -> None:
    if type(credential) is str and len(credential) > 2048:
        raise DomainError("REMOTE_CREDENTIAL_TOO_LONG", "令牌超过 2048 字符，请只粘贴令牌本身", "credential")
    if (
        type(credential) is not str
        or not credential
        or len(credential) > 2048
        or any(ord(character) <= 32 or ord(character) >= 127 for character in credential)
    ):
        raise DomainError(
            "REMOTE_CREDENTIAL_CHARACTERS", "令牌含空白或非英文字符，请只粘贴令牌本身", "credential"
        )


def normalize_credential(value: str | None) -> str | None:
    if value is None:
        return None
    value = re.sub(r"^Bearer\s+", "", value.strip(), flags=re.IGNORECASE).strip()
    if value:
        validate_credential(value)
    return value or None


class CredentialStore(Protocol):
    def put(self, reference: str, credential: str) -> None: ...

    def get(self, reference: str) -> str | None: ...

    def delete(self, reference: str) -> None: ...

    def masked_tail(self, reference: str) -> str | None: ...


@dataclass(slots=True)
class MemoryCredentialStore:
    """Explicit test implementation, never selected as a production fallback."""

    _values: dict[str, str] = field(default_factory=dict[str, str], init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def put(self, reference: str, credential: str) -> None:
        validate_reference(reference)
        validate_credential(credential)
        with self._lock:
            self._values[reference] = credential

    def get(self, reference: str) -> str | None:
        validate_reference(reference)
        with self._lock:
            return self._values.get(reference)

    def delete(self, reference: str) -> None:
        validate_reference(reference)
        with self._lock:
            self._values.pop(reference, None)

    def masked_tail(self, reference: str) -> str | None:
        credential = self.get(reference)
        return ("••••" + credential[-4:] if len(credential) > 4 else "••••") if credential else None
