"""Explicit Windows vault access with safe failures and reference-only targets."""

from typing import Protocol, cast

from htd.application.remote.ports import validate_credential, validate_reference
from htd.domain.common.errors import DomainError

_USERNAME = "HarnessTokenDeck"


class VaultBackend(Protocol):
    def set_password(self, service: str, username: str, password: str) -> None: ...

    def get_password(self, service: str, username: str) -> str | None: ...

    def delete_password(self, service: str, username: str) -> None: ...


def _unavailable() -> DomainError:
    return DomainError("CREDENTIAL_STORE_UNAVAILABLE", "Windows 凭据存储不可用，请检查当前用户的凭据管理器")


class WinVaultCredentialStore:
    def __init__(self, *, backend: VaultBackend | None = None) -> None:
        try:
            if backend is None:
                from keyring.backends.Windows import WinVaultKeyring

                vault = WinVaultKeyring()
                if vault.priority <= 0:
                    raise _unavailable()
                backend = cast(VaultBackend, vault)
            self._backend = backend
        except Exception:
            raise _unavailable() from None

    def put(self, reference: str, credential: str) -> None:
        validate_reference(reference)
        validate_credential(credential)
        try:
            self._backend.set_password(reference, _USERNAME, credential)
        except Exception:
            raise _unavailable() from None

    def get(self, reference: str) -> str | None:
        validate_reference(reference)
        try:
            return self._backend.get_password(reference, _USERNAME)
        except Exception:
            raise _unavailable() from None

    def delete(self, reference: str) -> None:
        validate_reference(reference)
        try:
            if self._backend.get_password(reference, _USERNAME) is not None:
                self._backend.delete_password(reference, _USERNAME)
        except Exception:
            raise _unavailable() from None

    def masked_tail(self, reference: str) -> str | None:
        credential = self.get(reference)
        return ("••••" + credential[-4:] if len(credential) > 4 else "••••") if credential else None


class LazyWinVaultCredentialStore:
    """A machine without a vault can still collect usage and manage local wallets."""

    def _vault(self) -> WinVaultCredentialStore:
        if not hasattr(self, "_store"):
            self._store = WinVaultCredentialStore()
        return self._store

    def put(self, reference: str, credential: str) -> None:
        self._vault().put(reference, credential)

    def get(self, reference: str) -> str | None:
        return self._vault().get(reference)

    def delete(self, reference: str) -> None:
        self._vault().delete(reference)

    def masked_tail(self, reference: str) -> str | None:
        return self._vault().masked_tail(reference)
