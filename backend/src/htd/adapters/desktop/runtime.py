"""Registry-only WebView2 preflight; importing it never loads CLR or a GUI."""

import re

_CLIENT = r"Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
DOWNLOAD_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"


def _registry_version(root: int, view: int) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(root, _CLIENT, 0, winreg.KEY_READ | view) as key:
            value, _ = winreg.QueryValueEx(key, "pv")
        if (
            isinstance(value, str)
            and re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", value)
            and any(int(part) > 0 for part in value.split("."))
        ):
            return value
    except OSError:
        pass
    return None


def webview2_version() -> str | None:
    import winreg

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            if result := _registry_version(root, view):
                return result
    return None
