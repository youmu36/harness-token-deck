"""Local CI snapshots include dirty source and never reuse stale or failed verification."""

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from scripts import local_ci
from scripts.source_snapshot import source_fingerprint


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "--quiet")
    (root / ".gitignore").write_text(".verify/\n.venv/\nnode_modules/\ndocs/\nrelease/\n", encoding="utf-8")
    (root / "source.py").write_text("synthetic original\n", encoding="utf-8")
    (root / "deleted.py").write_text("synthetic deleted\n", encoding="utf-8")
    git(root, "add", "--all")
    git(
        root,
        "-c",
        "user.name=Synthetic",
        "-c",
        "user.email=synthetic@example.test",
        "commit",
        "--quiet",
        "--no-gpg-sign",
        "-m",
        "synthetic initial snapshot",
    )
    return root


def test_fingerprint_tracks_working_tree_bytes_and_not_staging_or_private_files(checkout: Path) -> None:
    before = source_fingerprint(checkout)
    (checkout / "source.py").write_bytes(b"synthetic modified\r\n")
    changed = source_fingerprint(checkout)
    assert changed != before
    git(checkout, "add", "source.py")
    assert source_fingerprint(checkout) == changed
    (checkout / "docs").mkdir()
    (checkout / "docs/private.md").write_text("synthetic private", encoding="utf-8")
    assert source_fingerprint(checkout) == changed
    (checkout / "added.py").write_text("synthetic added", encoding="utf-8")
    assert source_fingerprint(checkout) != changed
    (checkout / "added.py").unlink()
    (checkout / "deleted.py").unlink()
    assert source_fingerprint(checkout) != changed


def test_isolated_snapshot_includes_add_modify_delete_and_has_independent_git_history(
    checkout: Path, tmp_path: Path
) -> None:
    (checkout / "source.py").write_bytes(b"synthetic staged\r\n")
    git(checkout, "add", "source.py")
    (checkout / "source.py").write_bytes(b"synthetic working tree\r\n")
    (checkout / "added.py").write_text("synthetic added", encoding="utf-8")
    (checkout / "deleted.py").unlink()
    (checkout / "docs").mkdir()
    (checkout / "docs/private.md").write_text("synthetic private", encoding="utf-8")
    workspace = tmp_path / "isolated"
    local_ci.snapshot(checkout, workspace, dict(os.environ))
    assert source_fingerprint(checkout) == source_fingerprint(workspace)
    assert (workspace / "source.py").read_bytes() == b"synthetic working tree\r\n"
    assert not (workspace / "deleted.py").exists() and not (workspace / "docs").exists()
    assert (workspace / ".git").is_dir()
    assert not (workspace / ".git/objects/info/alternates").exists()
    (workspace / "source.py").write_text("synthetic sandbox mutation", encoding="utf-8")
    assert (checkout / "source.py").read_bytes() == b"synthetic working tree\r\n"


@pytest.fixture
def fake_tools(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple[str, ...], Path, dict[str, str]]]:
    original_run = local_ci.run
    original_subprocess = cast(Callable[..., subprocess.CompletedProcess[Any]], subprocess.run)
    calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []

    def node_version(command: Any, *args: Any, **kwargs: Any) -> Any:
        if command == ["node", "--version"]:
            return subprocess.CompletedProcess(command, 0, stdout="v24.0.0\n")
        return original_subprocess(command, *args, **kwargs)

    def run(command: tuple[str, ...], root: Path, environment: dict[str, str]) -> None:
        if command[0] == "git":
            original_run(command, root, environment)
            return
        calls.append((command, root, environment))
        assert "HTD_SMOKE_ISOLATED" not in environment
        if "scripts.verify" in command:
            assert (root / "source.py").read_text(encoding="utf-8") == "synthetic original\n"
            assert Path(environment["HTD_DATA_DIR"]).is_relative_to(root.parent)
            report = root / ".verify/last.json"
            report.parent.mkdir()
            report.write_text(json.dumps({"status": "passed", "steps": []}), encoding="utf-8")

    monkeypatch.setattr(local_ci, "run", run)
    monkeypatch.setattr(subprocess, "run", node_version)
    return calls


@pytest.mark.skipif(os.name != "nt", reason="Local CI targets Windows")
def test_matching_results_reuse_and_source_additions_invalidate(
    checkout: Path, fake_tools: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "synthetic-parent-env")
    monkeypatch.setenv("VIRTUAL_ENV", "synthetic-parent-env")
    assert local_ci.verify(checkout)["status"] == "passed"
    assert len(fake_tools) == 3
    assert all("UV_PROJECT_ENVIRONMENT" not in environment for _, _, environment in fake_tools)
    assert all("VIRTUAL_ENV" not in environment for _, _, environment in fake_tools)
    assert not (checkout / ".venv").exists() and not (checkout / "node_modules").exists()
    assert local_ci.verify(checkout)["status"] == "passed"
    assert len(fake_tools) == 3
    (checkout / "added.py").write_text("synthetic new source", encoding="utf-8")
    local_ci.verify(checkout)
    assert len(fake_tools) == 6
    assert not list((checkout / ".verify/local-ci").glob("run-*"))


@pytest.mark.skipif(os.name != "nt", reason="Local CI targets Windows")
def test_forced_failure_invalidates_previous_success(
    checkout: Path, fake_tools: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    local_ci.verify(checkout)
    passing_run = local_ci.run

    def fail(command: tuple[str, ...], root: Path, environment: dict[str, str]) -> None:
        if "scripts.verify" in command:
            raise subprocess.CalledProcessError(1, command)
        passing_run(command, root, environment)

    monkeypatch.setattr(local_ci, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        local_ci.verify(checkout, force=True)
    report = json.loads((checkout / ".verify/local-ci/last.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    monkeypatch.setattr(local_ci, "run", passing_run)
    count = len(fake_tools)
    local_ci.verify(checkout)
    assert len(fake_tools) == count + 3


@pytest.mark.skipif(os.name != "nt", reason="Local CI targets Windows")
def test_source_changed_during_checks_cannot_produce_reusable_success(
    checkout: Path, fake_tools: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    passing_run = local_ci.run

    def change(command: tuple[str, ...], root: Path, environment: dict[str, str]) -> None:
        passing_run(command, root, environment)
        if "scripts.verify" in command:
            (checkout / "source.py").write_text("synthetic concurrent change", encoding="utf-8")

    monkeypatch.setattr(local_ci, "run", change)
    with pytest.raises(ValueError, match="Source changed during local CI"):
        local_ci.verify(checkout)
    report = json.loads((checkout / ".verify/local-ci/last.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
