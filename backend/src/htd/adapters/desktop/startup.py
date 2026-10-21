"""Current-user Windows startup; only changed by an explicit settings command."""

import subprocess
import sys
from contextlib import suppress

KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "HarnessTokenDeck"


def set_startup(enabled: bool) -> None:
    import winreg

    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command.extend(["-m", "htd"])
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(command))
        else:
            with suppress(FileNotFoundError):
                winreg.DeleteValue(key, NAME)
