"""Store parity, safe failures and bridge/log credential redaction."""

import json
import logging
import traceback
from collections.abc import Callable

import pytest
from htd.adapters.credentials.winvault import WinVaultCredentialStore
from htd.application.remote.ports import (
    CredentialStore,
    MemoryCredentialStore,
    credential_reference,
    normalize_credential,
)
from htd.domain.common.errors import DomainError
from htd.domain.common.redaction import redact_credentials
from htd.ipc.errors import exception_envelope

SECRET = "p10-synthetic-secret-1234"
REFERENCE = credential_reference("test-p10-unit")


def test_normalized_length_and_distinct_validation_errors() -> None:
    token = "x" * 2048
    assert normalize_credential(" \r\nBearer " + token + "\n ") == token
    assert normalize_credential(" \r\n ") is None
    for invalid, code in (
        ("x" * 2049, "REMOTE_CREDENTIAL_TOO_LONG"),
        ("inside space", "REMOTE_CREDENTIAL_CHARACTERS"),
        ("sk-中文", "REMOTE_CREDENTIAL_CHARACTERS"),
    ):
        with pytest.raises(DomainError) as failure:
            normalize_credential(invalid)
        assert failure.value.code == code


class FakeVault:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[service, username] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        del self.values[service, username]


@pytest.fixture(params=["memory", "vault"])
def store(request: pytest.FixtureRequest) -> CredentialStore:
    return (
        MemoryCredentialStore() if request.param == "memory" else WinVaultCredentialStore(backend=FakeVault())
    )


def test_store_lifecycle_and_mask(store: CredentialStore) -> None:
    assert store.get(REFERENCE) is None and store.masked_tail(REFERENCE) is None
    store.delete(REFERENCE)
    store.put(REFERENCE, SECRET)
    assert store.get(REFERENCE) == SECRET and store.masked_tail(REFERENCE) == "••••1234"
    assert SECRET not in repr(store)
    store.put(REFERENCE, "changed-value-5678")
    assert store.get(REFERENCE) == "changed-value-5678" and store.masked_tail(REFERENCE) == "••••5678"
    store.delete(REFERENCE)
    store.delete(REFERENCE)
    assert store.get(REFERENCE) is None


def test_mask_never_discloses_a_whole_short_secret(store: CredentialStore) -> None:
    store.put(REFERENCE, "1234")
    assert store.masked_tail(REFERENCE) == "••••"


@pytest.mark.parametrize("reference", ["other/account", "HarnessTokenDeck/remote/", REFERENCE + "/other"])
def test_invalid_reference_never_reaches_backend(store: CredentialStore, reference: str) -> None:
    with pytest.raises(DomainError):
        store.put(reference, SECRET)


@pytest.mark.parametrize("credential", ["", "Bearer test", "line\r\nbreak", "x" * 2049])
def test_invalid_credential_never_reaches_backend(store: CredentialStore, credential: str) -> None:
    with pytest.raises(DomainError):
        store.put(REFERENCE, credential)


class BrokenVault(FakeVault):
    def set_password(self, service: str, username: str, password: str) -> None:
        raise RuntimeError(SECRET)

    def get_password(self, service: str, username: str) -> str | None:
        raise RuntimeError(SECRET)


@pytest.mark.parametrize("operation", ["put", "get", "delete", "masked_tail"])
def test_backend_exception_never_exposes_secret(operation: str, caplog: pytest.LogCaptureFixture) -> None:
    store = WinVaultCredentialStore(backend=BrokenVault())
    calls: dict[str, Callable[[], object]] = {
        "put": lambda: store.put(REFERENCE, SECRET),
        "get": lambda: store.get(REFERENCE),
        "delete": lambda: store.delete(REFERENCE),
        "masked_tail": lambda: store.masked_tail(REFERENCE),
    }
    try:
        calls[operation]()
    except DomainError as error:
        assert error.code == "CREDENTIAL_STORE_UNAVAILABLE"
        envelope = exception_envelope(error)
        logging.getLogger("test.credential").error("%s", "".join(traceback.format_exception(error)))
    else:
        pytest.fail("Credential store unexpectedly available")
    assert SECRET not in json.dumps(envelope) and SECRET not in caplog.text


def test_vault_initialization_failure_has_no_backend_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from keyring.backends import Windows

    def fail() -> object:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(Windows, "WinVaultKeyring", fail)
    with pytest.raises(DomainError) as caught:
        WinVaultCredentialStore()
    assert caught.value.code == "CREDENTIAL_STORE_UNAVAILABLE"
    assert SECRET not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "message",
    [
        "Authorization: Bearer secret-value",
        '"api_key": "secret-value"',
        "access_token='secret-value'",
        "api-key=secret-value",
        "sk-secret-value",
        "Bearer secret-value",
    ],
)
def test_unexpected_exception_and_domain_envelope_redact_patterns(
    message: str, caplog: pytest.LogCaptureFixture
) -> None:
    assert "secret-value" not in redact_credentials(message)
    unexpected = exception_envelope(RuntimeError(message))
    business = exception_envelope(DomainError("REMOTE_ERROR", message))
    assert "secret-value" not in json.dumps(unexpected) + json.dumps(business) + caplog.text
