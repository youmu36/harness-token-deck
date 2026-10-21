"""Native mutex/AF_PIPE, credential ACLs, startup arbitration and smoke migration."""

import ctypes
import json
import os
import subprocess
import sys
import time
from contextlib import suppress
from ctypes import wintypes
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil
import pytest
from htd.adapters.desktop.credentials import key_path, read_key
from htd.adapters.desktop.single_instance import SingleInstance

pytestmark = [pytest.mark.desktop, pytest.mark.skipif(os.name != "nt", reason="Windows native APIs")]
ROOT = Path(__file__).resolve().parents[3]
HELPER = Path(__file__).with_name("instance_helper.py")


def environment() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(ROOT / "backend/src")}


def process_tree(process_id: int) -> list[psutil.Process]:
    # Windows venv python.exe is a redirector; the interpreter owns the window/files.
    try:
        root = psutil.Process(process_id)
        return [root, *root.children(recursive=True)]
    except psutil.NoSuchProcess:
        return []


def kill_tree(process: subprocess.Popen[str]) -> tuple[str, str]:
    for member in reversed(process_tree(process.pid)):
        with suppress(psutil.NoSuchProcess):
            member.kill()
    return process.communicate(timeout=5)


def private_acl(path: Path) -> str:
    # Read through a separate Windows API; no PowerShell profile/module dependencies.
    api: Any = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetFileSecurityW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    api.GetFileSecurityW.restype = wintypes.BOOL
    api.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.c_void_p,
    )
    api.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.restype = ctypes.c_void_p
    size = wintypes.DWORD()
    api.GetFileSecurityW(str(path), 4, None, 0, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    assert api.GetFileSecurityW(str(path), 4, buffer, size.value, ctypes.byref(size))
    text = wintypes.LPWSTR()
    assert api.ConvertSecurityDescriptorToStringSecurityDescriptorW(buffer, 1, 4, ctypes.byref(text), None)
    try:
        assert text.value is not None
        return text.value
    finally:
        kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def visible_window(process_id: int) -> bool:
    api: Any = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    api.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    api.EnumWindows.restype = wintypes.BOOL
    api.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    api.GetWindowThreadProcessId.restype = wintypes.DWORD
    api.IsWindowVisible.argtypes = (wintypes.HWND,)
    api.IsWindowVisible.restype = wintypes.BOOL
    owners = {member.pid for member in process_tree(process_id)}
    visible = False

    def visit(handle: int, parameter: int) -> bool:
        nonlocal visible
        owner = wintypes.DWORD()
        api.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        visible |= owner.value in owners and bool(api.IsWindowVisible(handle))
        return True

    api.EnumWindows(callback_type(visit), 0)
    return visible


@pytest.mark.parametrize("silent", [False, True])
def test_single_instance_second_process_ack_or_timeout_without_database(silent: bool) -> None:
    name = f"HTD.Test.{uuid4().hex}"
    directory = Path(os.environ["HTD_DATA_DIR"])
    owner = subprocess.Popen(
        [sys.executable, str(HELPER), "owner", name, *(["--silent"] if silent else [])],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment(),
        cwd=ROOT,
    )
    child: subprocess.Popen[str] | None = None
    try:
        assert owner.stdout is not None
        assert json.loads(owner.stdout.readline())["ready"]
        started = time.monotonic()
        child = subprocess.Popen(
            [sys.executable, str(HELPER), "launch", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment(),
            cwd=ROOT,
        )
        observed: set[str] = set()
        while child.poll() is None and time.monotonic() - started < 8:
            for member in process_tree(child.pid):
                with suppress(psutil.NoSuchProcess):
                    observed.update(file.path for file in member.open_files())
            time.sleep(0.01)
        stdout, stderr = child.communicate(timeout=2)
        assert child.returncode == (2 if silent else 0), stderr
        assert observed, "No file handles sampled from the secondary's actual interpreter"
        assert not any("htd.sqlite3" in path.lower() for path in observed)
        assert not (directory / "htd.sqlite3").exists()
        assert not (directory / "unexpected-bootstrap").exists()
        if silent:
            assert 3 <= time.monotonic() - started < 6
            assert "已在运行但没有响应" in json.loads(stdout)["dialog"]
        else:
            assert stdout == ""
    finally:
        if child is not None and child.poll() is None:
            kill_tree(child)
        if owner.stdin is not None:
            owner.stdin.close()
            owner.stdin = None
        _, owner_error = owner.communicate(timeout=5)
        assert owner.returncode == 0, owner_error


def test_single_instance_credentials_acl_startup_queue_auth_and_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = Path(os.environ["HTD_DATA_DIR"])
    name = f"HTD.Test.{uuid4().hex}"
    monkeypatch.setenv("HTD_TEST_HOOKS", "0")
    with SingleInstance(directory, name=name, pipe_name=name) as first:
        first.listen()
        key = key_path(directory, first.user_hash)
        pointer = directory / f".instance-{first.user_hash}.json"
        for path in (key, pointer):
            assert private_acl(path) == f"D:P(A;;FA;;;{first.sid})"
        with pytest.raises(AuthenticationError), Client(first.address, family="AF_PIPE", authkey=b"x" * 32):
            pass
        with Client(first.address, family="AF_PIPE", authkey=read_key(directory, first.user_hash)) as client:
            client.send_bytes(b'{"cmd":"activate","extra":true}')
            assert client.poll(1)
            assert json.loads(client.recv_bytes()) == {"ok": False}
        with SingleInstance(directory, name=name, pipe_name=name) as second:
            assert not second.primary
            for _ in range(3):
                assert second.forward()
            assert first.next_command() == "activate"
            assert first.next_command(timeout=0) is None
            assert not second.forward("test.trayOpen")
            assert second.forward("quit")
            assert first.quitting.is_set() and first.next_command() == "quit"
    assert not key.exists() and not pointer.exists()
    monkeypatch.setenv("HTD_TEST_HOOKS", "1")
    with SingleInstance(directory, name=name, pipe_name=name) as restarted:
        assert restarted.primary
        restarted.listen()
        assert restarted.forward("test.trayOpen")
        assert restarted.next_command() == "test.trayOpen"


def test_single_instance_silent_authentication_has_bounded_timeout_and_shutdown() -> None:
    directory = Path(os.environ["HTD_DATA_DIR"])
    name = f"HTD.Test.{uuid4().hex}"
    with SingleInstance(directory, name=name, pipe_name=name) as first:
        first.listen()
        with Client(first.address, family="AF_PIPE", authkey=None):
            started = time.monotonic()
            assert not first.forward(timeout=0.15)
            assert time.monotonic() - started < 0.5
            first.close()


def test_single_instance_smoke_migrates_handshakes_and_releases_resources() -> None:
    directory = Path(os.environ["HTD_DATA_DIR"])
    responses: list[object] = []
    for _ in range(2):
        completed = subprocess.run(
            [sys.executable, "-m", "htd", "--smoke"],
            cwd=ROOT,
            env=environment(),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        hello = json.loads(completed.stdout)
        assert hello["ok"] and hello["data"]["protocol"] == 1
        assert hello["data"]["dataDir"] == str(directory.resolve())
        capabilities = hello["data"]["capabilities"]
        assert {"app.hello", "app.sync", "stats.cacheMonitor"} <= set(capabilities)
        assert len(capabilities) == len(set(capabilities))
        responses.append(hello["data"]["revisions"])
    assert responses[0] == responses[1]
    assert (directory / "htd.sqlite3").is_file()
    assert (directory / "logs/htd.log").is_file()
    assert not (directory / "webview").exists()
    assert not list(directory.glob("instance-*.key"))


def test_single_instance_real_desktop_launch_and_authenticated_quit() -> None:
    if not (ROOT / "backend/src/htd/web/index.html").is_file():
        pytest.skip("Run npm run build before native desktop launch")
    directory = Path(os.environ["HTD_DATA_DIR"])
    name = f"HTD.Test.{uuid4().hex}"
    process = subprocess.Popen(
        [sys.executable, str(HELPER), "desktop", name],
        cwd=ROOT,
        env=environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while process.poll() is None and not visible_window(process.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if process.poll() is not None or not visible_window(process.pid):
            _, stderr = kill_tree(process)
            raise AssertionError(stderr + (directory / "logs/htd.log").read_text(encoding="utf-8"))
        with SingleInstance(directory, name=name, pipe_name=name) as secondary:
            assert not secondary.primary
            assert secondary.forward("activate")
            assert secondary.forward("quit")
        _, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert (directory / "htd.sqlite3").is_file()
        assert not list(directory.glob("instance-*.key"))
        with SingleInstance(directory, name=name, pipe_name=name) as subsequent:
            assert subsequent.primary
    finally:
        if process.poll() is None:
            kill_tree(process)
