"""Small, explicitly typed Win32 calls; importing this module acquires no resources."""

import ctypes
import os
from ctypes import wintypes
from pathlib import Path
from typing import Any, cast


def native_function(library: str, name: str, arguments: tuple[object, ...], result: object) -> Any:
    if os.name != "nt":
        raise OSError("桌面宿主仅支持 Windows")
    function = getattr(ctypes.WinDLL(library, use_last_error=True), name)
    function.argtypes = arguments
    function.restype = result
    return function


def close_handle(handle: int) -> None:
    close = native_function("kernel32", "CloseHandle", (wintypes.HANDLE,), wintypes.BOOL)
    if not close(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def enable_per_monitor_dpi() -> None:
    set_context = native_function(
        "user32", "SetProcessDpiAwarenessContext", (ctypes.c_void_p,), wintypes.BOOL
    )
    if not set_context(ctypes.c_void_p(-4)) and ctypes.get_last_error() != 5:
        raise ctypes.WinError(ctypes.get_last_error())


def allow_pipe_foreground(pipe_handle: int) -> None:
    server = native_function(
        "kernel32",
        "GetNamedPipeServerProcessId",
        (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)),
        wintypes.BOOL,
    )
    allow = native_function("user32", "AllowSetForegroundWindow", (wintypes.DWORD,), wintypes.BOOL)
    process_id = wintypes.DWORD()
    if server(pipe_handle, ctypes.byref(process_id)):
        allow(process_id.value)


def pipe_process(pipe_handle: int) -> int:
    """Hold the authenticated server process, so mutex release cannot race file replacement."""
    server = native_function(
        "kernel32",
        "GetNamedPipeServerProcessId",
        (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)),
        wintypes.BOOL,
    )
    process_id = wintypes.DWORD()
    if not server(pipe_handle, ctypes.byref(process_id)):
        raise ctypes.WinError(ctypes.get_last_error())
    opened = native_function(
        "kernel32", "OpenProcess", (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), wintypes.HANDLE
    )
    handle = opened(0x00100000, False, process_id.value)  # SYNCHRONIZE only.
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return cast(int, handle)


def wait_process(handle: int, seconds: float) -> bool:
    wait = native_function(
        "kernel32", "WaitForSingleObject", (wintypes.HANDLE, wintypes.DWORD), wintypes.DWORD
    )
    return wait(handle, int(seconds * 1000)) == 0


def foreground(handle: int) -> bool:
    focus = native_function("user32", "SetForegroundWindow", (wintypes.HWND,), wintypes.BOOL)
    if focus(handle):
        return True
    active = native_function("user32", "GetForegroundWindow", (), wintypes.HWND)
    owner = native_function(
        "user32", "GetWindowThreadProcessId", (wintypes.HWND, ctypes.c_void_p), wintypes.DWORD
    )
    current = native_function("kernel32", "GetCurrentThreadId", (), wintypes.DWORD)
    attach = native_function(
        "user32", "AttachThreadInput", (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL), wintypes.BOOL
    )
    gui_thread, active_thread = current(), owner(active(), None)
    if not active_thread or gui_thread == active_thread or not attach(gui_thread, active_thread, True):
        return False
    try:
        return bool(focus(handle))
    finally:
        attach(gui_thread, active_thread, False)


def _free(pointer: object) -> None:
    free = native_function("kernel32", "LocalFree", (ctypes.c_void_p,), ctypes.c_void_p)
    free(pointer)


class NamedMutex:
    def __init__(self, name: str) -> None:
        create = native_function(
            "kernel32", "CreateMutexW", (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR), wintypes.HANDLE
        )
        ctypes.set_last_error(0)
        handle = create(None, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.exists = ctypes.get_last_error() == 183
        self._handle: int | None = cast(int, handle)

    def close(self) -> None:
        if self._handle is not None:
            close_handle(self._handle)
            self._handle = None


def current_user_sid() -> str:
    current = native_function("kernel32", "GetCurrentProcess", (), wintypes.HANDLE)
    open_token = native_function(
        "advapi32",
        "OpenProcessToken",
        (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)),
        wintypes.BOOL,
    )
    information = native_function(
        "advapi32",
        "GetTokenInformation",
        (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)),
        wintypes.BOOL,
    )
    convert = native_function(
        "advapi32",
        "ConvertSidToStringSidW",
        (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)),
        wintypes.BOOL,
    )
    token = wintypes.HANDLE()
    if not open_token(current(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        information(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not information(token, 1, buffer, size.value, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first member is a PSID.
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not convert(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if text.value is None:
                raise OSError("无法读取当前用户 SID")
            return text.value
        finally:
            _free(ctypes.cast(text, ctypes.c_void_p))
    finally:
        close_handle(cast(int, token.value))


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]


def write_private_file(path: Path, data: bytes, sid: str) -> None:
    """CREATE_NEW with a protected, current-user-only DACL before writing any secret."""
    convert = native_function(
        "advapi32",
        "ConvertStringSecurityDescriptorToSecurityDescriptorW",
        (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p),
        wintypes.BOOL,
    )
    create = native_function(
        "kernel32",
        "CreateFileW",
        (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ),
        wintypes.HANDLE,
    )
    write = native_function(
        "kernel32",
        "WriteFile",
        (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p),
        wintypes.BOOL,
    )
    descriptor = ctypes.c_void_p()
    if not convert(f"D:P(A;;FA;;;{sid})", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        security = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        handle = create(str(path), 0x40000000, 0, ctypes.byref(security), 1, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            count = wintypes.DWORD()
            if not write(handle, data, len(data), ctypes.byref(count), None) or count.value != len(data):
                raise OSError("无法写入单实例凭据")
        finally:
            close_handle(cast(int, handle))
    finally:
        _free(descriptor)


def wake_pipe(address: str) -> None:
    """Wake Listener.accept without an unbounded authenticated Client call."""
    create = native_function(
        "kernel32",
        "CreateFileW",
        (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ),
        wintypes.HANDLE,
    )
    handle = create(address, 0xC0000000, 0, None, 3, 0x40000000, None)
    if handle != ctypes.c_void_p(-1).value:
        close_handle(cast(int, handle))


def show_error(message: str) -> None:
    show = native_function(
        "user32",
        "MessageBoxW",
        (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT),
        ctypes.c_int,
    )
    show(None, message, "Harness Token Deck", 0x10)
