"""Reject stale lock roots and versions that cannot fit Windows version resources."""

import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.set_version import ROOT, check_version, validate_version
from scripts.source_snapshot import source_fingerprint


def test_version_check_rejects_stale_npm_lock_root(tmp_path: Path) -> None:
    for filename in ("VERSION", "pyproject.toml", "package.json", "package-lock.json", "uv.lock"):
        shutil.copyfile(ROOT / filename, tmp_path / filename)
    assert check_version(tmp_path) == check_version(ROOT)
    path = tmp_path / "package-lock.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["packages"][""]["version"] = "0.3.0"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="Version mismatch"):
        check_version(tmp_path)


@pytest.mark.parametrize("version", ["0.4", "01.4.0", "0.4.0-beta", "0.4.65536"])
def test_versions_must_be_release_versions_with_valid_windows_components(version: str) -> None:
    with pytest.raises(ValueError, match=r"x\.y\.z"):
        validate_version(version)


@pytest.mark.parametrize("git_checkout", [True, False])
def test_build_records_installer_digest_and_keeps_source_archive_builds_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_checkout: bool
) -> None:
    monkeypatch.setattr(sys, "path", [str(ROOT / "scripts"), *sys.path])
    builder = importlib.import_module("build_release")
    version = check_version(ROOT)
    if git_checkout:
        subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("release/\n", encoding="utf-8")
    installer = tmp_path / f"release/Harness Token Deck_{version}_x64-setup.exe"
    installer.parent.mkdir()
    installer.write_bytes(b"synthetic installer")
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder, "check_version", lambda: version)

    def skip_command(*_: str) -> None:
        pass

    def frozen(_: str) -> dict[str, int]:
        return {"payload_bytes": 0}

    def build_installer(_: str) -> Path:
        return installer

    monkeypatch.setattr(builder, "run", skip_command)
    monkeypatch.setattr(builder, "build_frozen", frozen)
    monkeypatch.setattr(builder, "build_installer", build_installer)
    monkeypatch.setattr(builder, "smoke", lambda: {"exit_code": 0, "integrity": "ok"})
    monkeypatch.setattr(sys, "argv", ["build_release.py", "--reuse-checks"])
    assert builder.main() == 0
    report = json.loads((installer.parent / f"release-{version}.json").read_text(encoding="utf-8"))
    assert report["source_fingerprint"] == (source_fingerprint(tmp_path) if git_checkout else None)
    assert report["installer_sha256"] == builder.sha256(installer)
