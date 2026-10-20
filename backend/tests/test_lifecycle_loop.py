"""Leak detection must reject a slow repeated leak and tolerate bounded native fluctuations."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts.lifecycle_loop import LifecycleRun
from scripts.lifecycle_support import isolated_environment, process_tree, publish_status, trend


@pytest.mark.parametrize("values", [[500] * 100, [500 + index % 8 for index in range(100)]])
def test_bounded_resource_fluctuations_pass(values: list[int]) -> None:
    assert trend(values, 32)["passed"]


@pytest.mark.parametrize("values", [[500 + index for index in range(100)], [500] * 70 + [550] * 30])
def test_gradual_and_tail_resource_leaks_fail(values: list[int]) -> None:
    assert not trend(values, 32)["passed"]


def test_desktop_environment_replaces_inherited_real_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTU_SOURCE_ROOT_CODEX", "C:/real/codex")
    monkeypatch.setenv("HTU_DEBUG", "1")
    monkeypatch.setenv("HTU_DEV_URL", "http://localhost:9999")
    before = dict(os.environ)
    environment = isolated_environment(tmp_path / "loop")
    assert os.environ == before
    for key in ("HTU_DATA_DIR", "HTU_SOURCE_ROOT_CODEX", "CODEX_HOME", "APPDATA", "LOCALAPPDATA"):
        assert Path(environment[key]).is_relative_to(tmp_path / "loop")
    assert environment["HTU_DEBUG"] == "0" and "HTU_DEV_URL" not in environment


def test_process_cleanup_excludes_independent_ime_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        def __init__(self, name: str) -> None:
            self._name = name

        def name(self) -> str:
            return self._name

        def children(self, **_: Any) -> list["Process"]:
            return [Process("python.exe"), Process("msedgewebview2.exe"), Process("SGTool.exe")]

    def process_factory(_: int) -> Process:
        return Process("python.exe")

    monkeypatch.setattr("scripts.lifecycle_support.psutil.Process", process_factory)
    assert [process.name() for process in process_tree(1)] == [
        "python.exe",
        "python.exe",
        "msedgewebview2.exe",
    ]


def test_snapshot_retries_after_windows_reader_blocks_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "status.json"
    assert publish_status(destination, {"state": "hidden"})
    original = Path.replace

    def blocked_replace(source: Path, target: Path) -> Path:
        raise PermissionError("Windows reader has not closed the old frame")

    monkeypatch.setattr(Path, "replace", blocked_replace)
    assert not publish_status(destination, {"state": "visible"})
    assert json.loads(destination.read_text(encoding="utf-8")) == {"state": "hidden"}
    monkeypatch.setattr(Path, "replace", original)
    assert publish_status(destination, {"state": "visible"})
    assert json.loads(destination.read_text(encoding="utf-8")) == {"state": "visible"}


def test_secondary_ack_waits_for_gui_processing_with_stale_visible_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = object.__new__(LifecycleRun)
    run.data = tmp_path
    run.name = "synthetic-loop"
    run.environment = {}
    run.windows = MagicMock()
    run.process = MagicMock()
    run.process.poll.return_value = None  # type: ignore[attr-defined]
    run.status = {"state": "visible", "activation_generation": 7}
    refreshes = 0

    def refresh() -> None:
        nonlocal refreshes
        refreshes += 1
        if refreshes == 3:
            run.status["activation_generation"] = 8

    monkeypatch.setattr(run, "refresh", refresh)

    def no_sleep(seconds: float) -> None:
        pass

    def acknowledged(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["synthetic-secondary"], 0, stdout="", stderr="")

    monkeypatch.setattr("scripts.lifecycle_loop.time.sleep", no_sleep)
    monkeypatch.setattr("scripts.lifecycle_loop.subprocess.run", acknowledged)
    run.launch_secondary("visible", 1)
    assert refreshes == 3
    assert run.status["activation_generation"] == 8
