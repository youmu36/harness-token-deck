"""Verify concrete CLI workflows, failure reports, target activation, and read-only inspection."""

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from htu.domain.common.errors import DomainError
from htu.migration.__main__ import activate, main
from htu.migration.convert import convert
from htu.migration.legacy import reader

from .helpers import FIXTURES


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HTU_DATA_DIR", str(tmp_path / "app"))

    def stopped(*, detector: Callable[[], bool] | None = None) -> bool:
        return detector() if detector else False

    monkeypatch.setattr(reader, "legacy_running", stopped)
    return tmp_path / "app"


def test_inspect_snapshot_dry_run_verify_and_activate(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = FIXTURES / "legacy/v030.sqlite3"
    before = source.read_bytes()
    assert main(["inspect", str(source)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["userVersion"] == 6 and inspected["groups"] == 2 and inspected["vendors"] == 4
    assert not isolated.exists() and source.read_bytes() == before
    snapshot_dir = isolated / "migration/snapshot"
    assert main(["snapshot", str(source), "--output-dir", str(snapshot_dir)]) == 0
    frozen = Path(json.loads(capsys.readouterr().out)["path"])
    dry = isolated / "migration/dry"
    assert main(["import", str(frozen), "--dry-run", "--output-dir", str(dry)]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["ok"] and not imported["activated"]
    assert (dry / "staging.sqlite3").exists() and not (isolated / "htu.sqlite3").exists()
    assert main(["verify", str(frozen), str(dry / "staging.sqlite3")]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    live = isolated / "migration/live"
    assert main(["import", str(frozen), "--output-dir", str(live)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["activated"] and (isolated / "htu.sqlite3").exists()
    assert not (live / "staging.sqlite3").exists() and (live / "report.md").exists()
    assert source.read_bytes() == before


def test_failure_preserves_active_target_and_writes_report(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = isolated / "htu.sqlite3"
    convert(FIXTURES / "legacy/v030.sqlite3", target)
    before = target.read_bytes()
    directory = isolated / "migration/failure"
    assert main(["import", str(FIXTURES / "legacy/v022.sqlite3"), "--output-dir", str(directory)]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "MIGRATION_TARGET_NOT_EMPTY"
    assert target.read_bytes() == before
    assert "失败，禁止激活" in (directory / "report.md").read_text(encoding="utf-8")
    assert not (directory / "staging.sqlite3").exists()


def test_active_database_sidecars_refuse_activation(isolated: Path) -> None:
    target = isolated / "htu.sqlite3"
    target.parent.mkdir(parents=True)
    staging = isolated / "staging.sqlite3"
    convert(FIXTURES / "legacy/v022.sqlite3", staging)
    sidecar = target.with_name(target.name + "-wal")
    sidecar.write_bytes(b"keep")
    with pytest.raises(DomainError) as error:
        activate(staging, target)
    assert error.value.code == "MIGRATION_TARGET_BUSY"
    assert staging.exists() and sidecar.read_bytes() == b"keep" and not target.exists()


def test_actual_module_inspect_and_verify_entrypoints(isolated: Path) -> None:
    source = FIXTURES / "legacy/v023.sqlite3"
    staging = isolated / "staging.sqlite3"
    convert(source, staging)
    environment = os.environ | {"PYTHONIOENCODING": "utf-8"}
    for args in (["inspect", str(source)], ["verify", str(source), str(staging)]):
        result = subprocess.run(
            [sys.executable, "-m", "htu.migration", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert isinstance(json.loads(result.stdout), dict)


def test_output_path_cannot_replace_database_or_existing_report(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = FIXTURES / "legacy/v022.sqlite3"
    staging = isolated / "staging.sqlite3"
    convert(source, staging)
    before = staging.read_bytes()
    assert main(["verify", str(source), str(staging), "--report", str(staging)]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "MIGRATION_OUTPUT_INVALID"
    assert staging.read_bytes() == before
    assert main(["import", str(source), "--dry-run", "--output-dir", str(isolated)]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "MIGRATION_OUTPUT_INVALID"
    assert staging.read_bytes() == before
