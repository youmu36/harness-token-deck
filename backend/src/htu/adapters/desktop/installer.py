"""Installer shortcut identity without loading the desktop host or CLR."""

import ctypes
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any

APP_ID = "HarnessTokenUsage.Desktop"


class Guid(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]

    def __init__(self, value: str) -> None:
        super().__init__((ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le))


class PropertyKey(ctypes.Structure):
    _fields_ = [("guid", Guid), ("pid", wintypes.DWORD)]


class PropVariant(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_ushort),
        ("reserved", ctypes.c_ushort * 3),
        ("value", ctypes.c_void_p),
        ("padding", ctypes.c_void_p),
    ]


def _checked(result: int) -> None:
    if result < 0:
        raise OSError(f"COM error 0x{result & 0xFFFFFFFF:08x}")


def shortcut_app_id(path: Path, *, write: bool = False) -> str:
    ole: Any = ctypes.OleDLL("ole32")
    ole.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    ole.CoInitializeEx.restype = ctypes.c_long
    _checked(ole.CoInitializeEx(None, 2))
    store = ctypes.c_void_p()
    variant = PropVariant()
    try:
        shell: Any = ctypes.WinDLL("shell32")
        shell.SHGetPropertyStoreFromParsingName.argtypes = (
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(Guid),
            ctypes.POINTER(ctypes.c_void_p),
        )
        shell.SHGetPropertyStoreFromParsingName.restype = ctypes.c_long
        iid = Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")
        _checked(
            shell.SHGetPropertyStoreFromParsingName(
                str(path.resolve()), None, 2 if write else 0, ctypes.byref(iid), ctypes.byref(store)
            )
        )
        table = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        key = PropertyKey(Guid("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3"), 5)
        method = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PropertyKey), ctypes.POINTER(PropVariant)
        )(table[6 if write else 5])
        if write:
            text = ctypes.create_unicode_buffer(APP_ID)
            variant.type, variant.value = 31, ctypes.cast(text, ctypes.c_void_p).value
            _checked(method(store, ctypes.byref(key), ctypes.byref(variant)))
            _checked(ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(table[7])(store))
            return APP_ID
        _checked(method(store, ctypes.byref(key), ctypes.byref(variant)))
        return ctypes.wstring_at(variant.value) if variant.type == 31 and variant.value else ""
    finally:
        if store.value:
            if not write:
                ole.PropVariantClear(ctypes.byref(variant))
            table = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(table[2])(store)
        ole.CoUninitialize()
