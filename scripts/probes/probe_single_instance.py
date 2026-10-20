"""Verify a named mutex and authenticated activation between two real processes."""

import argparse
import ctypes
import json
import secrets
import subprocess
import sys
import threading
import uuid
from ctypes import wintypes
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any


def mutex(name: str) -> tuple[Any, bool]:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle, ctypes.get_last_error() == 183


def close_mutex(handle: Any) -> None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle(handle)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--name", default=f"HTU.Probe.{uuid.uuid4().hex}")
    parser.add_argument("--key")
    parser.add_argument("--output", type=Path, default=Path(".verify/probe-single-instance.json"))
    args = parser.parse_args()
    name = str(args.name)
    address = rf"\\.\pipe\{name}"
    key = bytes.fromhex(str(args.key)) if args.child else secrets.token_bytes(32)
    handle, exists = mutex(rf"Local\{name}")
    try:
        if args.child:
            assert exists, "Child failed to detect first instance"
            with Client(address, family="AF_PIPE", authkey=key) as client:
                client.send({"cmd": "activate"})
                assert client.poll(3), "ACK timed out"
                assert client.recv() == {"ok": True}
            return 0
        assert not exists
        received: list[object] = []
        with Listener(address, family="AF_PIPE", authkey=key) as listener:

            def serve() -> None:
                with listener.accept() as connection:
                    received.append(connection.recv())
                    connection.send({"ok": True})

            server = threading.Thread(target=serve, daemon=True)
            server.start()
            child = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--child",
                    "--name",
                    name,
                    "--key",
                    key.hex(),
                ],
                check=False,
                timeout=10,
                capture_output=True,
                text=True,
            )
            server.join(timeout=3)
        assert child.returncode == 0, child.stderr
        assert received == [{"cmd": "activate"}]
    finally:
        close_mutex(handle)
    handle, still_exists = mutex(rf"Local\{name}")
    close_mutex(handle)
    assert not still_exists, "Mutex leaked after both instances exited"
    report = {
        "passed": True,
        "second_instance_ack": True,
        "mutex_released": True,
        "scope": "Unique probe names; persistent credential ACLs deferred to P5",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
