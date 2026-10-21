"""Temporary EcoQoS and memory priority for this WebView's own processes."""

import ctypes
from ctypes import wintypes

from htd.adapters.desktop._win32 import close_handle, native_function


class _Throttling(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]


def reclaim_working_set(pid: int) -> None:
    """Release unused resident pages without changing execution or memory priority."""
    opened = native_function(
        "kernel32", "OpenProcess", (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), wintypes.HANDLE
    )
    handle = opened(0x1100, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        empty = native_function("kernel32", "K32EmptyWorkingSet", (wintypes.HANDLE,), wintypes.BOOL)
        if not empty(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close_handle(handle)


class BackgroundProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        opened = native_function(
            "kernel32", "OpenProcess", (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), wintypes.HANDLE
        )
        self.handle = opened(0x1200, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._get = native_function(
            "kernel32",
            "GetProcessInformation",
            (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD),
            wintypes.BOOL,
        )
        self._set = native_function(
            "kernel32",
            "SetProcessInformation",
            (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD),
            wintypes.BOOL,
        )
        self.power = _Throttling(1, 0, 0)
        self.priority = wintypes.ULONG(5)
        try:
            if not self._get(
                self.handle, 4, ctypes.byref(self.power), ctypes.sizeof(self.power)
            ) or not self._get(self.handle, 0, ctypes.byref(self.priority), ctypes.sizeof(self.priority)):
                raise ctypes.WinError(ctypes.get_last_error())
            low = _Throttling(1, self.power.ControlMask | 1, self.power.StateMask | 1)
            if not self._set(self.handle, 4, ctypes.byref(low), ctypes.sizeof(low)):
                raise ctypes.WinError(ctypes.get_last_error())
            memory = wintypes.ULONG(1)
            if not self._set(self.handle, 0, ctypes.byref(memory), ctypes.sizeof(memory)):
                self._set(self.handle, 4, ctypes.byref(self.power), ctypes.sizeof(self.power))
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            close_handle(self.handle)
            raise

    def reclaim(self) -> None:
        # A held process handle keeps its PID from being reused. Request quota
        # access only for this one background transition; retain the original
        # priority handle even when optional working-set reclamation is denied.
        reclaim_working_set(self.pid)

    def restore(self) -> None:
        try:
            self._set(self.handle, 4, ctypes.byref(self.power), ctypes.sizeof(self.power))
            self._set(self.handle, 0, ctypes.byref(self.priority), ctypes.sizeof(self.priority))
        finally:
            close_handle(self.handle)
