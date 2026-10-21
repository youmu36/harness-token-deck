"""Real Windows vault writes use only uniquely named P10 test targets."""

import sys
from uuid import uuid4

import pytest
from htd.adapters.credentials.winvault import WinVaultCredentialStore
from htd.application.remote.ports import credential_reference

pytestmark = [pytest.mark.desktop, pytest.mark.skipif(sys.platform != "win32", reason="Windows vault")]


def test_real_winvault_write_read_overwrite_delete() -> None:
    reference = credential_reference("test-p10-1-" + uuid4().hex)
    secret = "synthetic-p10-vault-" + uuid4().hex
    store = WinVaultCredentialStore()
    try:
        assert store.get(reference) is None
        store.put(reference, secret)
        assert store.get(reference) == secret
        assert store.masked_tail(reference) == "••••" + secret[-4:]
        replacement = secret + "-5678"
        store.put(reference, replacement)
        assert store.get(reference) == replacement
        assert store.masked_tail(reference) == "••••5678"
        store.delete(reference)
        assert store.get(reference) is None
        store.delete(reference)
    finally:
        store.delete(reference)
    assert store.get(reference) is None
