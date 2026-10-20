"""Reject stale lock roots and versions that cannot fit Windows version resources."""

import json
import shutil
from pathlib import Path

import pytest

from scripts.set_version import ROOT, check_version, validate_version


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
