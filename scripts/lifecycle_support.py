"""Win32 observations and process-tree resource analysis for source lifecycle acceptance."""

import ctypes
import json
import os
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path
from typing import Any

import psutil


def publish_status(destination: Path, status: dict[str, Any]) -> bool:
    """Keep the last complete frame if a Windows reader temporarily denies file replacement."""
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(status), encoding="utf-8")
    try:
        temporary.replace(destination)
    except PermissionError:
        return False
    return True


class IconIdentifier(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("window", wintypes.HWND),
        ("identifier", wintypes.UINT),
        ("guid", ctypes.c_byte * 16),
    ]


class Windows:
    def __init__(self) -> None:
        self.api: Any = ctypes.WinDLL("user32", use_last_error=True)
        for name in ("IsWindow", "IsWindowVisible", "IsIconic"):
            function = getattr(self.api, name)
            function.argtypes, function.restype = (wintypes.HWND,), wintypes.BOOL
        self.api.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        self.api.PostMessageW.restype = wintypes.BOOL
        self.api.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        self.api.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
        self.api.GetForegroundWindow.restype = wintypes.HWND
        self.shell: Any = ctypes.WinDLL("shell32", use_last_error=True)
        self.shell.Shell_NotifyIconGetRect.argtypes = (
            ctypes.POINTER(IconIdentifier),
            ctypes.POINTER(wintypes.RECT),
        )
        self.shell.Shell_NotifyIconGetRect.restype = ctypes.c_long

    def icon_present(self, handle: int, identifier: int) -> bool:
        icon = IconIdentifier()
        icon.size, icon.window, icon.identifier = ctypes.sizeof(icon), handle, identifier
        rectangle = wintypes.RECT()
        return self.shell.Shell_NotifyIconGetRect(ctypes.byref(icon), ctypes.byref(rectangle)) == 0

    def visible(self, handle: int) -> bool:
        return bool(self.api.IsWindowVisible(handle)) and not bool(self.api.IsIconic(handle))

    def close_window(self, handle: int) -> None:
        if not self.api.PostMessageW(handle, 0x0010, 0, 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def grant_foreground(self) -> None:
        self.api.AllowSetForegroundWindow(0xFFFFFFFF)


def process_tree(process_id: int) -> list[psutil.Process]:
    try:
        root = psutil.Process(process_id)
        # IMEs may spawn independent daemons under WebView2. Never kill or budget those.
        members = [root, *root.children(recursive=True)]
        result: list[psutil.Process] = []
        for member in members:
            with suppress(psutil.NoSuchProcess):
                if member.name().lower() in {"python.exe", "pythonw.exe", "msedgewebview2.exe"}:
                    result.append(member)
        return result
    except psutil.NoSuchProcess:
        return []


def resource_sample(root_pid: int, owner_pid: int) -> dict[str, Any]:
    members = process_tree(root_pid)
    owner = psutil.Process(owner_pid)
    rows: list[dict[str, Any]] = []
    for member in members:
        with suppress(psutil.NoSuchProcess):
            rows.append(
                {
                    "pid": member.pid,
                    "name": member.name(),
                    "threads": member.num_threads(),
                    "handles": member.num_handles(),
                    "rss": member.memory_info().rss,
                }
            )
    # A Windows venv python.exe redirector is an ancestor, not a second application instance.
    descendants = {member.pid for member in owner.children(recursive=True)}
    python_count = sum(
        row["name"].lower().startswith("python")
        for row in rows
        if row["pid"] == owner_pid or row["pid"] in descendants
    )
    return {
        "application_processes": python_count,
        "tree_processes": len(rows),
        "tree_handles": sum(row["handles"] for row in rows),
        "tree_threads": sum(row["threads"] for row in rows),
        "owner_handles": owner.num_handles(),
        "owner_threads": owner.num_threads(),
        "processes": rows,
    }


def trend(values: list[int], allowance: int) -> dict[str, Any]:
    """Warm-up excluded by the caller; bounded tail growth and regression catch repeated leaks."""
    if not values:
        raise ValueError("No resource samples")
    width = max(1, len(values) // 5)

    def median(sample: list[int]) -> float:
        ordered = sorted(sample)
        middle = len(ordered) // 2
        return (ordered[middle] + ordered[~middle]) / 2

    growth = median(values[-width:]) - median(values[:width])
    center = (len(values) - 1) / 2
    variance = sum((index - center) ** 2 for index in range(len(values)))
    slope = (
        sum((index - center) * value for index, value in enumerate(values)) / variance if variance else 0.0
    )
    return {
        "minimum": min(values),
        "maximum": max(values),
        "first": values[0],
        "last": values[-1],
        "median_growth": growth,
        "slope_per_cycle": round(slope, 4),
        "allowance": allowance,
        "passed": growth <= allowance and slope * max(0, len(values) - 1) <= allowance,
    }


def isolated_environment(directory: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "USERPROFILE",
        "HOME",
        "APPDATA",
        "LOCALAPPDATA",
        "CODEX_HOME",
        "HTU_DATA_DIR",
        "HTU_SOURCE_ROOT_CODEX",
        "HTU_SOURCE_ROOT_ZCODE",
        "HTU_SOURCE_ROOT_DSH",
    ):
        destination = directory / name.lower()
        destination.mkdir(parents=True, exist_ok=True)
        environment[name] = str(destination)
    environment.pop("HTU_DEV_URL", None)
    environment.update(HTU_TEST_HOOKS="1", HTU_DEBUG="0", PYTHONIOENCODING="utf-8")
    return environment
