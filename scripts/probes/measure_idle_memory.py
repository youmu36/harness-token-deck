"""Short, opt-in frozen-host memory samples with isolated empty sources; no working-set trim."""

import argparse
import ctypes
import json
import os
import subprocess
import time
from ctypes import wintypes
from pathlib import Path
from uuid import uuid4

import psutil
from htd.adapters.desktop._win32 import NamedMutex

ROOT = Path(__file__).resolve().parents[2]


def window_for(pid: int) -> int:
    found: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(window: int, _: int) -> bool:
        owner = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == pid and ctypes.windll.user32.IsWindowVisible(window):
            found.append(window)
        return True

    ctypes.windll.user32.EnumWindows(callback_type(visit), 0)
    return found[0] if found else 0


def sample(pid: int, stage: str) -> dict[str, object]:
    host = psutil.Process(pid)
    rows: list[dict[str, object]] = []
    for process in [host, *host.children(recursive=True)]:
        try:
            info = process.memory_info()
            try:
                private_working = process.memory_full_info().uss
            except psutil.AccessDenied:
                private_working = None
            rows.append(
                {
                    "pid": process.pid,
                    "name": process.name(),
                    "rssBytes": info.rss,
                    "privateCommitBytes": info.private,
                    "privateWorkingSetBytes": private_working,
                }
            )
        except psutil.NoSuchProcess:
            pass
    return {
        "stage": stage,
        "processes": rows,
        "hostRssMiB": round(host.memory_info().rss / 1024**2, 2),
        "hostPrivateWorkingSetMiB": round(host.memory_full_info().uss / 1024**2, 2),
        "hostPrivateWorkingSetMB": round(host.memory_full_info().uss / 1_000_000, 2),
        "sumRssMiB": round(sum(int(str(row["rssBytes"])) for row in rows) / 1024**2, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--isolated-instance", action="store_true")
    parser.add_argument(
        "--executable", type=Path, default=ROOT / "dist/HarnessTokenDeck/HarnessTokenDeck.exe"
    )
    args = parser.parse_args()
    mutex = NamedMutex(r"Local\HarnessTokenDeck.Instance")
    try:
        if mutex.exists and not args.isolated_instance:
            raise RuntimeError(
                "An existing desktop instance is running; isolated measurement was not started"
            )
    finally:
        mutex.close()
    directory = ROOT / ".verify/idle-memory" / (args.label + "-" + uuid4().hex)
    directory.mkdir(parents=True)
    environment = dict(os.environ, HTD_DATA_DIR=str(directory), HTD_DEBUG="0", HTD_TEST_HOOKS="1")
    if args.isolated_instance:
        environment["HTD_DESKTOP_ISOLATED"] = "1"
    for name in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "CODEX_HOME"):
        path = directory / name.lower()
        path.mkdir()
        environment[name] = str(path)
    for name in ("CLAUDE", "CODEX", "ZCODE", "DSH"):
        path = directory / name.lower()
        path.mkdir()
        environment["HTD_SOURCE_ROOT_" + name] = str(path)
    process = subprocess.Popen(
        [str(args.executable.resolve())], env=environment, creationflags=subprocess.CREATE_NO_WINDOW
    )
    report: dict[str, object] = {"label": args.label, "directory": str(directory), "samples": []}
    try:
        deadline = time.monotonic() + 20
        window = 0
        while process.poll() is None and time.monotonic() < deadline:
            window = window_for(process.pid)
            if window:
                break
            time.sleep(0.1)
        if not window:
            raise RuntimeError("The isolated frozen window did not open")
        time.sleep(5)
        samples = [sample(process.pid, "visible")]
        ctypes.windll.user32.PostMessageW(wintypes.HWND(window), 0x10, 0, 0)
        time.sleep(2)
        if ctypes.windll.user32.IsWindowVisible(wintypes.HWND(window)):
            raise RuntimeError("The isolated window did not hide")
        samples.append(sample(process.pid, "tray+2s"))
        time.sleep(8)
        samples.append(sample(process.pid, "tray+10s"))
        report["samples"] = samples
        report["ok"] = True
    finally:
        if process.poll() is None:
            subprocess.run(
                [str(args.executable.resolve()), "--quit"], env=environment, timeout=10, check=False
            )
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                psutil.Process(process.pid).kill()
        (directory / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (ROOT / f".verify/idle-memory-{args.label}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
