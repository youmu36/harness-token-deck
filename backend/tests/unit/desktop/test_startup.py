"""Startup ordering and failure paths without a GUI or native dialogs."""

import json
import logging
import os
import sys
import threading
from pathlib import Path
from types import TracebackType
from typing import Any

import htu.__main__ as entry
import pytest
from htu.adapters.desktop.host import frontend_url
from htu.adapters.desktop.logging_setup import logging_session


class Instance:
    primary = True
    acknowledged = True

    def __init__(self, directory: Path, calls: list[str]) -> None:
        assert (directory / "logs").is_dir()
        self.calls = calls
        self.quitting = threading.Event()
        calls.append("instance")

    def listen(self) -> None:
        self.calls.append("listen")

    def forward(self, command: str) -> bool:
        self.calls.append(command)
        return self.acknowledged

    def __enter__(self) -> "Instance":
        return self

    def __exit__(
        self, kind: type[BaseException] | None, value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.calls.append("release")


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sequence: list[str] = []

    def instance(directory: Path) -> Instance:
        return Instance(directory, sequence)

    def notify(message: str) -> None:
        sequence.append("dialog")

    def desktop(*args: object, **kwargs: object) -> None:
        sequence.append("desktop")

    monkeypatch.setattr(entry, "SingleInstance", instance)
    monkeypatch.setattr(entry, "show_error", notify)

    def runtime() -> str:
        sequence.append("runtime")
        return "154.0.0.0"

    class Application:
        def __init__(self, directory: Path, *, start_scheduler: bool = True) -> None:
            sequence.append("bootstrap" if start_scheduler else "smoke")
            self.bridge: Any = self
            self.window_state: Any = self

        def close(self) -> None:
            pass

        def release_idle_memory(self) -> None:
            pass

        def call(self, command: str, payload: object) -> dict[str, object]:
            sequence.append("hello")
            assert command == "app.hello" and payload == {"clientProtocol": 1}
            return {"ok": True, "data": {"protocol": 1}}

        def __enter__(self) -> "Application":
            return self

        def __exit__(
            self,
            kind: type[BaseException] | None,
            value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            sequence.append("close-app")

    monkeypatch.setattr(entry, "Application", Application)
    monkeypatch.setattr(entry, "webview2_version", runtime)
    monkeypatch.setattr(entry, "run_host", desktop)
    return sequence


def test_startup_orders_arbitration_runtime_bootstrap_and_cleanup(calls: list[str]) -> None:
    assert entry.main([]) == 0
    assert calls == ["instance", "listen", "runtime", "bootstrap", "desktop", "close-app", "release"]


@pytest.mark.parametrize("acknowledged,code", [(True, 0), (False, 2)])
def test_secondary_never_checks_runtime_or_bootstraps(
    calls: list[str], monkeypatch: pytest.MonkeyPatch, acknowledged: bool, code: int
) -> None:
    monkeypatch.setattr(Instance, "primary", False)
    monkeypatch.setattr(Instance, "acknowledged", acknowledged)
    assert entry.main([]) == code
    assert calls == ["instance", "activate", *([] if acknowledged else ["dialog"]), "release"]


def test_missing_runtime_exits_before_database(calls: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entry, "webview2_version", lambda: None)
    assert entry.main([]) == 1
    assert calls == ["instance", "listen", "dialog", "release"]
    assert not (Path(os.environ["HTU_DATA_DIR"]) / "htu.sqlite3").exists()


def test_smoke_skips_runtime_gui_and_scheduler(calls: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(["--smoke"]) == 0
    assert calls == ["instance", "smoke", "hello", "close-app", "release"]
    assert json.loads(capsys.readouterr().out)["ok"]


def test_isolated_release_smoke_uses_directory_namespace(
    calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = entry.SingleInstance
    observed: dict[str, str] = {}

    def isolated(directory: Path, **kwargs: str) -> object:
        observed.update(kwargs)
        return original(directory)

    monkeypatch.setenv("HTU_SMOKE_ISOLATED", "1")
    monkeypatch.setattr(entry, "SingleInstance", isolated)
    assert entry.main(["--smoke"]) == 0
    assert calls == ["instance", "smoke", "hello", "close-app", "release"]
    assert observed["name"].startswith("HarnessTokenUsage.Smoke.")
    assert observed["pipe_name"] == observed["name"]


def test_windowed_smoke_without_standard_streams(calls: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert entry.main(["--smoke"]) == 0
    assert calls == ["instance", "smoke", "hello", "close-app", "release"]


def test_isolated_desktop_requires_test_hooks_and_uses_its_own_instance(
    calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = entry.SingleInstance
    observed: dict[str, str] = {}

    def isolated(directory: Path, **kwargs: str) -> object:
        observed.update(kwargs)
        return original(directory)

    monkeypatch.setenv("HTU_TEST_HOOKS", "1")
    monkeypatch.setenv("HTU_DESKTOP_ISOLATED", "1")
    monkeypatch.setattr(entry, "SingleInstance", isolated)
    assert entry.main([]) == 0
    assert observed["name"].startswith("HarnessTokenUsage.Test.")
    assert observed["pipe_name"] == observed["name"]


def test_quit_waits_until_owner_releases_instance(calls: list[str]) -> None:
    assert entry.main(["--quit", "--wait-for-exit"]) == 0
    assert calls == ["instance", "release", "instance", "release"]


def test_installer_acceptance_shares_its_own_namespace_for_backup_quit_and_gui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "installer"
    root.mkdir()
    monkeypatch.setenv("HTU_INSTALLER_TEST_ROOT", str(root))
    directory = root / "data"
    assert entry._instance_options(directory) == {}
    (root / ".htu-installer-test").touch()
    options = entry._instance_options(directory)
    assert options["name"].startswith("HarnessTokenUsage.Installer.")
    assert entry._instance_options(directory, smoke=True) == options
    assert entry._instance_options(tmp_path / "real-data") == {}


def test_quit_without_owner_does_not_bootstrap(calls: list[str]) -> None:
    assert entry.main(["--quit"]) == 0
    assert calls == ["instance", "release"]


def test_host_failure_closes_app_and_lock(calls: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    def failed(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic host failure")

    monkeypatch.setattr(entry, "run_host", failed)
    assert entry.main([]) == 1
    assert calls == ["instance", "listen", "runtime", "bootstrap", "close-app", "release", "dialog"]


def test_log_rotation_and_handler_cleanup(tmp_path: Path) -> None:
    logger = logging.getLogger("htu.startup-test")
    root = logging.getLogger()
    original = list(root.handlers)
    with logging_session(tmp_path, debug=True) as path:
        handler: Any = root.handlers[-1]
        assert handler.maxBytes == 5 * 1024 * 1024 and handler.backupCount == 5
        handler.maxBytes = 200
        for _ in range(7):
            logger.info("synthetic startup record %s", "x" * 110)
        assert path.is_file()
        assert len(list(path.parent.glob("htu.log.*"))) == 5
    assert root.handlers == original
    path.unlink()


def test_development_url_requires_debug_and_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTU_DEV_URL", "http://127.0.0.1:5173")
    assert frontend_url(debug=True) == "http://127.0.0.1:5173"
    monkeypatch.setenv("HTU_DEV_URL", "https://example.com")
    with pytest.raises(ValueError, match="本机 HTTP"):
        frontend_url(debug=True)

    def exists(path: Path) -> bool:
        return True

    content = b"first frontend build"

    def read_bytes(path: Path) -> bytes:
        return content

    monkeypatch.setattr(Path, "is_file", exists)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    first = frontend_url(debug=False)
    assert first.split("?")[0].endswith(str(Path("web/index.html")))
    assert "?build=" in first
    assert frontend_url(debug=False) == first
    content = b"rebuilt frontend, same application version"
    assert frontend_url(debug=False) != first
