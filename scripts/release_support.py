"""Frozen payload audit, smoke acceptance, and installer tool discovery."""

import hashlib
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist/HarnessTokenDeck"
FORBIDDEN = {
    "tests",
    "test",
    "testing",
    "fixtures",
    "probes",
    "pytest",
    "_pytest",
    "hypothesis",
    "respx",
    "psutil",
}


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def audit_payload(directory: Path = DIST) -> dict[str, Any]:
    readers: Any = importlib.import_module("PyInstaller.archive.readers")
    paths = sorted(path for path in directory.rglob("*") if path.is_file())
    names = [path.relative_to(directory).as_posix() for path in paths]
    executable = directory / "HarnessTokenDeck.exe"
    archive: Any = readers.CArchiveReader(str(executable))
    modules: list[str] = []
    for name, entry in archive.toc.items():
        modules.append(name)
        if entry[-1] == "z":
            nested = archive.open_embedded_archive(name)
            modules.extend(nested.toc)
    forbidden = [
        name
        for name in [*names, *modules]
        if any(
            part.lower() in FORBIDDEN or part.lower().startswith(("pytest-", "hypothesis-", "_hypothesis_"))
            for part in name.replace("/", ".").replace("\\", ".").split(".")
        )
    ]
    if forbidden:
        raise ValueError(f"Development payload found: {forbidden}")
    required = [
        "_internal/htd/web/index.html",
        "_internal/htd/adapters/desktop/resources/app.ico",
        "_internal/THIRD_PARTY_NOTICES.md",
        "_internal/LICENSE",
    ]
    if any(not (directory / name).is_file() for name in required):
        raise ValueError("Frozen payload is missing web assets, icon, or licenses")
    if not list((directory / "_internal/htd/adapters/sqlite/migrations").glob("*.sql")):
        raise ValueError("Frozen payload is missing database migrations")
    if not list((directory / "_internal/webview/lib").rglob("Microsoft.Web.WebView2.*.dll")):
        raise ValueError("Frozen payload is missing WebView2 SDK DLLs")
    return {"bytes": sum(path.stat().st_size for path in paths), "files": names, "modules": sorted(modules)}


def isolated_environment(root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["HTD_SMOKE_ISOLATED"] = "1"
    for name in ("HTD_DEBUG", "HTD_DEV_URL", "HTD_TEST_HOOKS", "HTD_INSTALLER_TEST_ROOT"):
        environment.pop(name, None)
    for name in (
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "CODEX_HOME",
        "HTD_DATA_DIR",
        "HTD_SOURCE_ROOT_CODEX",
        "HTD_SOURCE_ROOT_ZCODE",
        "HTD_SOURCE_ROOT_DSH",
        "HTD_SOURCE_ROOT_CLAUDE",
        "CLAUDE_CONFIG_DIR",
    ):
        path = root / name.lower()
        path.mkdir(parents=True, exist_ok=True)
        environment[name] = str(path)
    return environment


def smoke(executable: Path = DIST / "HarnessTokenDeck.exe") -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="htd-release-smoke-") as temporary:
        root = Path(temporary)
        environment = isolated_environment(root)
        completed = subprocess.run([str(executable), "--smoke"], env=environment, check=False, timeout=15)
        if completed.returncode:
            raise RuntimeError(f"Frozen smoke exited {completed.returncode}")
        directory = Path(environment["HTD_DATA_DIR"])
        with closing(sqlite3.connect(directory / "htd.sqlite3")) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            migrations = connection.execute("SELECT COUNT(*) FROM schema_migration").fetchone()
        log = (directory / "logs/htd.log").read_text(encoding="utf-8")
        if integrity != ("ok",) or not migrations or migrations[0] < 1 or "握手与资源关闭完成" not in log:
            raise RuntimeError("Frozen smoke did not finish migrations, handshake and cleanup")
        return {
            "exit_code": completed.returncode,
            "database_version": migrations[0],
            "integrity": integrity[0],
        }


def find_makensis() -> Path:
    explicit = os.environ.get("HTD_MAKENSIS")
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise FileNotFoundError(f"HTD_MAKENSIS is not a file: {path}")
        return path
    found = shutil.which("makensis")
    candidates = [Path(found)] if found else []
    candidates += [
        Path(os.environ.get("LOCALAPPDATA", "")) / "tauri/NSIS/makensis.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "NSIS/makensis.exe",
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "NSIS/makensis.exe",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("NSIS unavailable; set HTD_MAKENSIS")


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
