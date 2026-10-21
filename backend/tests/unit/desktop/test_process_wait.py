"""Installer quit waits on the authenticated process handle, beyond mutex release."""

from pathlib import Path
from typing import Any

import htd.adapters.desktop.single_instance as module
import pytest


@pytest.mark.parametrize("exited", [True, False])
def test_authenticated_quit_wait_holds_process_before_sending_and_always_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exited: bool,
) -> None:
    calls: list[str] = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args: Any):
            pass

        def fileno(self):
            return 123

        def send_bytes(self, value):
            calls.append("quit")

        def poll(self, timeout):
            return True

        def recv_bytes(self, maximum):
            return b'{"ok":true}'

    def process(handle):
        assert handle == 123
        calls.append("open process")
        return 456

    def wait(handle, seconds):
        assert handle == 456 and seconds == 10
        calls.append("wait exit")
        return exited

    monkeypatch.setattr(module, "Client", lambda *args, **kwargs: Connection())
    monkeypatch.setattr(module, "read_key", lambda *args: b"key")
    monkeypatch.setattr(module, "answer_challenge", lambda *args: None)
    monkeypatch.setattr(module, "deliver_challenge", lambda *args: None)
    monkeypatch.setattr(module, "pipe_process", process)
    monkeypatch.setattr(module, "wait_process", wait)
    monkeypatch.setattr(module, "close_handle", lambda handle: calls.append("close handle"))
    instance = object.__new__(module.SingleInstance)
    instance.directory, instance.user_hash, instance.address = tmp_path, "user", "test pipe"
    assert instance.forward("quit", wait_for_exit=True) is exited
    assert calls == ["open process", "quit", "wait exit", "close handle"]
