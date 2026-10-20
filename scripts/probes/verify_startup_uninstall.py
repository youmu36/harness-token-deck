"""Single install/uninstall check: remove only the isolated startup registration."""

import json
import subprocess
import winreg
from uuid import uuid4

from scripts.release_support import ROOT, isolated_environment

UNINSTALL = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\HarnessTokenUsage"
RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
TEST_RUN = r"Software\HarnessTokenUsage-Acceptance\Run"
NAME = "HarnessTokenUsage"


def registration(key: str) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ) as handle:
            return str(winreg.QueryValueEx(handle, NAME)[0])
    except FileNotFoundError:
        return None


def main() -> None:
    for key in (UNINSTALL + "-Acceptance", TEST_RUN):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key):
                raise RuntimeError("An earlier acceptance registry key exists; inspect it first")
        except FileNotFoundError:
            pass
    original = registration(RUN)
    root = ROOT / ".verify/installer-startup" / uuid4().hex
    root.mkdir(parents=True)
    (root / ".htu-installer-test").write_text("isolated acceptance", encoding="ascii")
    environment = isolated_environment(root)
    environment["HTU_INSTALLER_TEST_ROOT"] = str(root)
    environment["HTU_DATA_DIR"] = str(root / "data")
    target = root / "program"
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    installer = ROOT / f"release/Harness Token Usage_{version}_x64-setup.exe"
    command = subprocess.list2cmdline([str(installer), "/S"]) + f" /D={target}"
    subprocess.run(command, env=environment, timeout=120, check=True)
    assert (target / "HarnessTokenUsage.exe").is_file()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, TEST_RUN) as handle:
        winreg.SetValueEx(handle, NAME, 0, winreg.REG_SZ, "isolated probe")
    command = subprocess.list2cmdline([str(target / "Uninstall.exe"), "/S", "/NOBACKUP"])
    subprocess.run(command + f" _?={target}", env=environment, timeout=120, check=True)
    assert registration(TEST_RUN) is None
    assert registration(RUN) == original
    assert not (target / "HarnessTokenUsage.exe").exists()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_RUN) as handle:
        assert winreg.QueryInfoKey(handle)[:2] == (0, 0)
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, TEST_RUN)
    report = {
        "passed": True,
        "startupRemoved": True,
        "realStartupUnchanged": True,
        "programRemoved": True,
        "directory": str(root),
    }
    (ROOT / ".verify/feedback-installer-startup.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
