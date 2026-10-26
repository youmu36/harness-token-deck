"""Startup resolution never falls back from a selected unavailable or foreign dataset."""

import json
from pathlib import Path

import pytest
from htd.adapters.data_directory import (
    DataDirectoryError,
    atomic_json,
    ensure_dataset,
    locator_path,
    resolve_data_directory,
)
from htd.bootstrap import Application


def test_precedence_and_isolated_override_never_rewrites_locator(tmp_path: Path) -> None:
    env = {"LOCALAPPDATA": str(tmp_path / "local")}
    default = resolve_data_directory(environ=env)
    assert default.root == tmp_path / "local/HarnessTokenDeck" and default.source == "default"
    selected = resolve_data_directory(tmp_path / "中文 空格", environ=env)
    identifier = ensure_dataset(selected)
    with Application(selected.root, start_scheduler=False):
        pass
    record = {"format": 1, "root": str(selected.root), "datasetId": identifier, "phase": "active"}
    atomic_json(locator_path(env), record)
    assert resolve_data_directory(environ=env).root == selected.root
    overridden = {**env, "HTD_DATA_DIR": str(tmp_path / "environment")}
    assert resolve_data_directory(environ=overridden).source == "environment"
    explicit = resolve_data_directory(tmp_path / "test", environ=overridden)
    assert explicit.source == "explicit" and explicit.root == tmp_path / "test"
    ensure_dataset(explicit)
    assert json.loads(locator_path(env).read_text(encoding="utf-8")) == record


@pytest.mark.parametrize("failure", ["missing", "foreign", "format", "phase", "json"])
def test_selected_invalid_path_is_explicit_error_without_default_database(
    tmp_path: Path, failure: str
) -> None:
    env = {"LOCALAPPDATA": str(tmp_path / "local")}
    target = resolve_data_directory(tmp_path / "selected", environ=env)
    identifier = ensure_dataset(target)
    (target.root / "htd.sqlite3").touch()
    record = {"format": 1, "root": str(target.root), "datasetId": identifier, "phase": "active"}
    if failure == "missing":
        (target.root / "htd.sqlite3").unlink()
    elif failure == "foreign":
        record["datasetId"] = "foreign"
    elif failure == "format":
        record["format"] = 2
    elif failure == "phase":
        record["phase"] = "unknown"
    atomic_json(locator_path(env), record)
    if failure == "json":
        locator_path(env).write_text("{", encoding="utf-8")
    with pytest.raises(DataDirectoryError):
        resolve_data_directory(environ=env)
    assert not (tmp_path / "local/HarnessTokenDeck/htd.sqlite3").exists()


def test_handshake_reports_effective_source_and_root(tmp_path: Path) -> None:
    with Application(tmp_path / "test", start_scheduler=False) as app:
        response = app.bridge.call("app.hello", {"clientProtocol": 1})
        assert response["ok"]
        assert response["data"]["dataDir"] == str(app.data_dir)
        assert response["data"]["dataDirSource"] == "explicit"
