"""Accept the actual NSIS package using isolated registry, data and shortcut paths."""

import argparse
import ctypes
import json
import sqlite3
import subprocess
import time
import uuid
import winreg
from contextlib import closing
from ctypes import wintypes
from pathlib import Path
from typing import Any

import psutil
from htd.adapters.desktop.installer import APP_ID, shortcut_app_id
from release_support import ROOT, isolated_environment, write_report
from set_version import check_version

KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\HarnessTokenDeck"


def visible_window(process_id: int) -> bool:
    api: Any = ctypes.WinDLL("user32")
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    api.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    api.IsWindowVisible.argtypes = (wintypes.HWND,)
    api.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    api.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    found = False

    def inspect(handle: int, _: int) -> bool:
        nonlocal found
        owner = wintypes.DWORD()
        api.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value == process_id and api.IsWindowVisible(handle):
            name = ctypes.create_unicode_buffer(256)
            api.GetClassNameW(handle, name, 256)
            found |= name.value.startswith("WindowsForms10.")
        return True

    api.EnumWindows(callback_type(inspect), 0)
    return found


def registry(key: str) -> dict[str, Any]:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY
        ) as handle:
            return {
                name: value
                for name, value, _ in (
                    winreg.EnumValue(handle, i) for i in range(winreg.QueryInfoKey(handle)[1])
                )
            }
    except FileNotFoundError:
        return {}


def execute(
    executable: Path, arguments: list[str], environment: dict[str, str], *, tail: str = "", expected: int = 0
) -> None:
    command = subprocess.list2cmdline([str(executable), *arguments]) + tail
    completed = subprocess.run(command, env=environment, check=False, timeout=120)
    if completed.returncode != expected:
        raise RuntimeError(f"{executable.name} exited {completed.returncode}; expected {expected}")


def marker(database: Path) -> str:
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        return str(connection.execute("SELECT value FROM release_acceptance").fetchone()[0])


def verify(installer: Path) -> dict[str, Any]:
    if registry(KEY + "-Acceptance"):
        raise RuntimeError("An earlier acceptance installation still exists; inspect its InstallLocation")
    original = registry(KEY)
    original_legacy = registry(KEY.replace("HarnessTokenDeck", "Harness Token Usage"))
    root = ROOT / f".verify/installer-runs/{uuid.uuid4().hex[:12]}"
    root.mkdir(parents=True)
    (root / ".htd-installer-test").write_text("isolated acceptance\n", encoding="ascii")
    environment = isolated_environment(root)
    environment["HTD_INSTALLER_TEST_ROOT"] = str(root)
    environment["HTD_DATA_DIR"] = str(root / "data")
    install_dir = root / "Program Files/Harness Token Deck"
    executable = install_dir / "HarnessTokenDeck.exe"
    database = root / "data/htd.sqlite3"
    report: dict[str, Any] = {"directory": str(root), "checks": [], "passed": False}

    def check(name: str, condition: bool = True) -> None:
        if not condition:
            raise RuntimeError(name)
        report["checks"].append(name)

    def install(*options: str) -> None:
        execute(installer, ["/S", *options], environment, tail=f" /D={install_dir}")
        check(
            "installed executable and uninstaller",
            executable.is_file() and (install_dir / "Uninstall.exe").is_file(),
        )
        check(
            "isolated current-user registration",
            registry(KEY + "-Acceptance").get("InstallLocation") == str(install_dir),
        )
        shortcut = root / "start-menu/Harness Token Deck.lnk"
        check(
            "Start Menu shortcut AppUserModelID", shortcut.is_file() and shortcut_app_id(shortcut) == APP_ID
        )

    def uninstall(*options: str, expected: int = 0) -> None:
        execute(
            install_dir / "Uninstall.exe",
            ["/S", *options],
            environment,
            tail=f" _?={install_dir}",
            expected=expected,
        )
        if expected == 0:
            check(
                "program payload removed",
                not executable.exists() and not (install_dir / "_internal").exists(),
            )
            check(
                "uninstall registry and shortcuts removed",
                not registry(KEY + "-Acceptance")
                and not (root / "start-menu/Harness Token Deck.lnk").exists(),
            )
            (install_dir / "Uninstall.exe").unlink(missing_ok=True)
            # _?= keeps this test's uninstaller in place so exit codes can be
            # observed. Model NSIS's normal temporary-copy self cleanup here.
            if install_dir.exists() and not any(install_dir.iterdir()):
                install_dir.rmdir()

    try:
        install()
        uninstall("/DELETEDATA")
        check(
            "never-launched uninstall deletes residual data without an empty backup",
            not (root / "data").exists() and not list((root / "desktop").glob("*.sqlite3")),
        )
        install()
        execute(executable, ["--smoke"], environment)
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE release_acceptance (value TEXT NOT NULL)")
            connection.execute("INSERT INTO release_acceptance VALUES ('preserved ledger')")
            connection.commit()
        sentinel = install_dir / "unrelated-user-file.txt"
        sentinel.write_text("keep\n", encoding="ascii")
        (install_dir / "_internal/obsolete-runtime-cache.tmp").write_text(
            "owned stale cache\n", encoding="ascii"
        )
        with subprocess.Popen([str(executable)], env=environment) as running:
            children: list[psutil.Process] = []
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and running.poll() is None:
                    children = [
                        child
                        for child in psutil.Process(running.pid).children(recursive=True)
                        if child.name().lower() == "msedgewebview2.exe"
                    ]
                    if visible_window(running.pid) and children:
                        break
                    time.sleep(0.1)
                check(
                    "frozen WinForms window and WebView2 render process",
                    visible_window(running.pid) and bool(children),
                )
                install()
                check(
                    "overwrite closes running application through authenticated quit pipe",
                    running.wait(timeout=10) == 0,
                )
                _, alive = psutil.wait_procs(children, timeout=10)
                check("WebView2 processes exit before overwrite completes", not alive)
            finally:
                if running.poll() is None:
                    for child in children:
                        if child.is_running():
                            child.kill()
                    running.kill()
                    running.wait(timeout=10)
        check("overwrite preserves ledger", marker(database) == "preserved ledger")
        install("/UNINSTALLFIRST")
        check(
            "uninstall-first preserves ledger and unrelated files",
            marker(database) == "preserved ledger" and sentinel.is_file(),
        )
        uninstall("/BACKUPDATA")
        check(
            "default uninstall preserves data and unrelated files",
            marker(database) == "preserved ledger" and sentinel.is_file(),
        )
        backups = list((root / "desktop").glob("htd-*.sqlite3"))
        check(
            "optional export also works while retaining data",
            len(backups) == 1 and marker(backups[0]) == "preserved ledger",
        )
        backups[0].unlink()
        install()
        execute(executable, ["--smoke"], environment)
        check("reinstall restores access to retained data", marker(database) == "preserved ledger")
        original_bytes = database.read_bytes()
        database.write_bytes(b"invalid database for backup failure acceptance")
        uninstall("/DELETEDATA", expected=1)
        check(
            "failed backup aborts uninstall and data deletion",
            executable.is_file()
            and database.read_bytes() == b"invalid database for backup failure acceptance"
            and bool(registry(KEY + "-Acceptance")),
        )
        database.write_bytes(original_bytes)
        uninstall("/DELETEDATA")
        backups = list((root / "desktop").glob("htd-*.sqlite3"))
        check(
            "delete-data saves a valid desktop backup first",
            len(backups) == 1 and marker(backups[0]) == "preserved ledger" and not (root / "data").exists(),
        )
        report["backup"] = str(backups[0])
        sentinel.unlink()
        install()
        execute(executable, ["--smoke"], environment)
        uninstall("/DELETEDATA", "/NOBACKUP")
        check("explicit no-backup deletion removes all application data", not (root / "data").exists())
        check("empty dedicated install directory is removed", not install_dir.exists())
        check(
            "existing new and legacy registrations unchanged",
            registry(KEY) == original
            and registry(KEY.replace("HarnessTokenDeck", "Harness Token Usage")) == original_legacy,
        )
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        write_report(ROOT / ".verify/installer.json", report)
    return report


def verify_manifest(version: str) -> None:
    manifest = json.loads((ROOT / f"release/release-{version}.json").read_text(encoding="utf-8"))
    installer = ROOT / "release" / manifest["installer"]
    assert manifest["version"] == version
    assert manifest["checks"] in ("frontend functionality passed", "reused targeted checks"), (
        "Debug build has no function checks"
    )
    assert manifest["installer_bytes"] == installer.stat().st_size
    assert manifest["smoke"]["exit_code"] == 0
    print(f"Build metadata matches version {version}; frozen startup passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-only", action="store_true")
    args = parser.parse_args()
    version = check_version()
    if args.manifest_only:
        verify_manifest(version)
    else:
        print(json.dumps(verify(ROOT / f"release/Harness Token Deck_{version}_x64-setup.exe"), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
