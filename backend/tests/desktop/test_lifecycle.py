"""Native WM_CLOSE, minimize, authenticated tray menu restore and shutdown observations."""

import ctypes
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from htu.adapters.desktop.single_instance import SingleInstance

from .test_single_instance import ROOT, environment, kill_tree

pytestmark = [pytest.mark.desktop, pytest.mark.skipif(os.name != "nt", reason="Windows native APIs")]


def wait_until(condition: Callable[[], bool], process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 15
    while process.poll() is None and not condition() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert process.poll() is None and condition()


def observations(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_lifecycle_close_tray_restore_hidden_fifty_events_and_disposal() -> None:
    directory = Path(os.environ["HTU_DATA_DIR"])
    output = directory / "observations.jsonl"
    name = f"HTU.Test.{uuid4().hex}"
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("lifecycle_helper.py")), name],
        cwd=ROOT,
        env={**environment(), "HTU_TEST_HOOKS": "1", "HTU_DEBUG": "0"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    api: Any = ctypes.WinDLL("user32", use_last_error=True)
    api.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    api.IsWindowVisible.argtypes = (wintypes.HWND,)
    api.IsIconic.argtypes = (wintypes.HWND,)
    api.GetForegroundWindow.restype = wintypes.HWND
    api.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    api.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
    try:
        wait_until(lambda: any(row["kind"] == "ready" for row in observations(output)), process)
        handle = next(row["handle"] for row in observations(output) if row["kind"] == "window")
        assert handle is not None and api.IsWindowVisible(handle)
        assert all(row["devtools"] is False for row in observations(output) if row["kind"] == "ready")
        assert [row for row in observations(output) if row["kind"] == "tray"] == [
            {"kind": "tray", "visible": True}
        ]
        commands = directory / "native-command.txt"
        commands.write_text("shell", encoding="utf-8")
        wait_until(lambda: any(row["kind"] == "shell" for row in observations(output)), process)
        shell = next(row["view"] for row in observations(output) if row["kind"] == "shell")
        assert shell["title"] == "Codex 用量" and shell["navigation"] == 7
        assert "本机账本" in shell["environment"]
        commands.write_text("navigate-settings", encoding="utf-8")
        wait_until(lambda: not commands.exists(), process)
        # Read the actual DOM through WebView2 before hiding, rather than changing backend preferences.
        commands.write_text("shell", encoding="utf-8")
        wait_until(
            lambda: any(
                row["kind"] == "shell" and row["view"]["title"] == "设置" for row in observations(output)
            ),
            process,
        )
        with SingleInstance(directory, name=name, pipe_name=name) as secondary:
            assert not secondary.primary
            before_hide = len([row for row in observations(output) if row["kind"] == "evaluate"])
            api.PostMessageW(handle, 0x0010, 0, 0)
            wait_until(lambda: any(row["kind"] == "hidden" for row in observations(output)), process)
            assert not api.IsWindowVisible(handle)
            assert len([row for row in observations(output) if row["kind"] == "evaluate"]) == before_hide
            commands.write_text("preferences", encoding="utf-8")
            wait_until(lambda: any(row["kind"] == "preferences" for row in observations(output)), process)
            saved = next(row["snapshot"] for row in observations(output) if row["kind"] == "preferences")
            assert saved["ok"] and saved["data"]["preferences"]["lastPage"] == "settings"
            api.AllowSetForegroundWindow(0xFFFFFFFF)
            assert secondary.forward("test.trayOpen")
            wait_until(lambda: api.IsWindowVisible(handle) and api.GetForegroundWindow() == handle, process)
            wait_until(
                lambda: any(
                    event["name"] == "usage.changed"
                    for row in observations(output)
                    if row["kind"] == "evaluate"
                    for event in row["events"]
                ),
                process,
            )
            api.ShowWindow(handle, 6)
            wait_until(lambda: bool(api.IsIconic(handle)), process)
            api.AllowSetForegroundWindow(0xFFFFFFFF)
            assert secondary.forward("activate")
            wait_until(lambda: not api.IsIconic(handle) and api.GetForegroundWindow() == handle, process)
            commands = directory / "native-command.txt"
            commands.write_text("events", encoding="utf-8")
            wait_until(lambda: any(row["kind"] == "received" for row in observations(output)), process)
            received = next(row["events"] for row in observations(output) if row["kind"] == "received")
            assert received == [
                {"name": "app.visibility", "payload": {"visible": True}},
                {"name": "usage.changed", "payload": {"harnesses": ["codex"], "revision": 50}},
            ]
            commands.write_text("pause", encoding="utf-8")
            wait_until(
                lambda: any(
                    row == {"kind": "tray-state", "paused": True, "busy": False}
                    for row in observations(output)
                ),
                process,
            )
            count = len(observations(output))
            commands.write_text("pause", encoding="utf-8")
            wait_until(
                lambda: any(
                    row == {"kind": "tray-state", "paused": False, "busy": False}
                    for row in observations(output)[count:]
                ),
                process,
            )
            count = len(observations(output))
            commands.write_text("scan", encoding="utf-8")
            wait_until(
                lambda: any(
                    row == {"kind": "tray-state", "paused": False, "busy": False}
                    for row in observations(output)[count:]
                ),
                process,
            )
            ready_count = len([row for row in observations(output) if row["kind"] == "ready"])
            commands.write_text("retry", encoding="utf-8")
            wait_until(
                lambda: len([row for row in observations(output) if row["kind"] == "ready"]) > ready_count,
                process,
            )
            assert len([row for row in observations(output) if row["kind"] == "tray"]) == 1
            assert [row for row in observations(output) if row["kind"] == "dialog"] == [
                {"kind": "dialog", "choice": "retry"}
            ]
            assert len([row for row in observations(output) if row["kind"] == "retry"]) == 1
            commands.write_text("navigate-wallet", encoding="utf-8")
            wait_until(lambda: not commands.exists(), process)
            commands.write_text("shell", encoding="utf-8")
            wait_until(
                lambda: any(
                    row["kind"] == "shell" and row["view"]["title"] == "供应商与钱包"
                    for row in observations(output)
                ),
                process,
            )
            commands.write_text("quit-host", encoding="utf-8")
        _, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        rows = observations(output)
        pushes = [row for row in rows if row["kind"] == "evaluate"]
        assert all(row["visible"] for row in pushes)
        usage = [event for row in pushes for event in row["events"] if event["name"] == "usage.changed"]
        assert usage == [{"name": "usage.changed", "payload": {"harnesses": ["codex"], "revision": 50}}]
        assert any(
            event == {"name": "app.visibility", "payload": {"visible": True}}
            for row in pushes
            for event in row["events"]
        )
        assert rows[-1] == {"kind": "tray-disposed", "disposed": True, "visible": False}
        assert not list(directory.glob("instance-*.key"))
        with sqlite3.connect(directory / "htu.sqlite3") as connection:
            saved_page = connection.execute(
                "SELECT value_json FROM preference WHERE key = 'lastPage'"
            ).fetchone()
            assert saved_page is not None and json.loads(saved_page[0]) == "wallet"
        with SingleInstance(directory, name=name, pipe_name=name) as next_owner:
            assert next_owner.primary
    finally:
        if process.poll() is None:
            _, stderr = kill_tree(process)
