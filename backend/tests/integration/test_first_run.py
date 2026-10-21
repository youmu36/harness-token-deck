"""Fresh installations work without accounts and keep pre-publication data isolated."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from htd.adapters.credentials.winvault import LazyWinVaultCredentialStore
from htd.bootstrap import Application
from htd.ipc.dto.app import HelloResponse
from htd.ipc.dto.preferences import PreferencesResponse
from htd.ipc.dto.wallet import WalletListResponse

from backend.tests.contracts.command_helpers import invoke


def test_first_run_and_restart_need_no_account_or_previous_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HTD_DATA_DIR")
    previous = Path(os.environ["LOCALAPPDATA"]) / "HarnessTokenUsage"
    previous.mkdir()
    old_database = previous / "htu.sqlite3"
    old_database.write_bytes(b"pre-publication data must remain untouched")
    monkeypatch.setenv("HTU_DATA_DIR", str(previous))

    def unavailable(_: LazyWinVaultCredentialStore) -> None:
        pytest.fail("Local first run must not request remote credentials")

    monkeypatch.setattr(LazyWinVaultCredentialStore, "_vault", unavailable)
    directory = Path(os.environ["LOCALAPPDATA"]) / "HarnessTokenDeck"
    with Application(start_scheduler=False) as app:
        assert app.data_dir == directory
        assert app.writer.path == directory / "htd.sqlite3"
        hello = invoke(app, "app.hello", {"clientProtocol": 1}, HelloResponse)
        assert Path(hello.dataDir) == directory
        assert invoke(app, "wallet.list", {}, WalletListResponse).vendors == []
        settings = invoke(app, "preferences.get", {}, PreferencesResponse)
        assert not settings.preferences.startAtLogin
        invoke(
            app,
            "preferences.update",
            {
                "operationId": str(uuid4()),
                "expectedRevision": settings.revision,
                "patch": {"theme": "dark"},
            },
            PreferencesResponse,
        )
    with Application(start_scheduler=False) as app:
        assert invoke(app, "preferences.get", {}, PreferencesResponse).preferences.theme == "dark"
        with app.readers.connection() as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("SELECT value FROM meta WHERE key='app_id'").fetchone()[0] == (
                "com.harness-token-deck.ledger"
            )
    assert old_database.read_bytes() == b"pre-publication data must remain untouched"
