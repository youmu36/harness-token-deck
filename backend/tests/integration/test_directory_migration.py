"""Move boundaries preserve every table, WAL, pending inbox and original backups."""

import tempfile
from pathlib import Path
from typing import Never, cast
from uuid import uuid4

import pytest
from htd.adapters import data_migration
from htd.adapters.data_cleanup import remove_application_data
from htd.adapters.data_directory import DataDirectoryError, DataLocation, resolve_data_directory
from htd.adapters.data_migration import recover_unactivated_directory
from htd.adapters.sqlite.dataset_snapshot import fingerprint, snapshot
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.bootstrap import Application
from htd.domain.common.testing import FakeClock
from htd.ipc.dto.base import Failure

from .collection_helpers import AT, usage


def location(tmp_path: Path) -> DataLocation:
    return DataLocation(tmp_path / "原始 数据", "default", tmp_path / "local/HarnessTokenDeck.location.json")


def stage(app: Application, count: int = 1) -> None:
    source = next(s for s in app.store.sources().sources if s.harness.value == "codex")
    app.writer.execute(
        lambda work: IngestRepository(work.connection).commit_batch(
            work,
            source.harness,
            source.path,
            source.path + "/synthetic.jsonl",
            {"offset": 1},
            tuple(usage(index) for index in range(1, count + 1)),
            at=AT,
        )
    )


def call(app: Application, command: str, **payload: object) -> dict[str, object]:
    reply = app.bridge.call(command, payload)
    assert reply["ok"], reply
    return reply["data"]  # type: ignore[return-value]


def test_move_preserves_facts_pending_backup_restart_and_old_fence(tmp_path: Path) -> None:
    selected = location(tmp_path)
    destination = tmp_path / "新目录 空格"
    with Application(data_location=selected, clock=FakeClock(AT), start_scheduler=False) as app:
        stage(app, 2)  # Deliberately leave accepted usage unbilled.
        backups = selected.root / "backups"
        backups.mkdir()
        original = backups / "original.sqlite3"
        original.write_bytes(b"synthetic old backup")
        modified = original.stat().st_mtime_ns
        expected = fingerprint(selected.root / "htd.sqlite3")
        preview = call(app, "dataDirectory.preview", target=str(destination))
        params = {
            "target": str(destination),
            "fingerprint": preview["fingerprint"],
            "operationId": str(uuid4()),
        }
        result = call(app, "dataDirectory.move", **params)
        assert result["restartRequired"] and app.directory_changed
        assert fingerprint(destination / "htd.sqlite3") == expected
        assert call(app, "dataDirectory.move", **params) == result
        assert (
            Failure.model_validate(
                app.bridge.call("collection.scanNow", {"operationId": str(uuid4())})
            ).error.code
            == "APP_RESTART_REQUIRED"
        )
        assert (destination / "backups/original.sqlite3").read_bytes() == original.read_bytes()
        assert (destination / "backups/original.sqlite3").stat().st_mtime_ns == modified
    with pytest.raises(DataDirectoryError, match="已迁移"):
        Application(selected.root, start_scheduler=False)
    resolved = resolve_data_directory(environ={"LOCALAPPDATA": str(selected.locator.parent)})
    assert resolved.source == "locator"
    with Application(data_location=resolved, clock=FakeClock(AT), start_scheduler=False) as app:
        assert call(app, "dataDirectory.status")["canCleanup"]
        unrelated = selected.root / "user-notes.txt"
        unrelated.write_text("keep", encoding="utf-8")
        assert cast(int, call(app, "dataDirectory.cleanup", operationId=str(uuid4()))["deletedFiles"]) >= 2
        assert unrelated.read_text(encoding="utf-8") == "keep"
        assert not (selected.root / "htd.sqlite3").exists()
        assert (destination / "htd.sqlite3").is_file()


def test_copy_failure_resumes_original_and_same_operation_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = location(tmp_path)
    destination = tmp_path / "target"
    with Application(data_location=selected, clock=FakeClock(AT), start_scheduler=False) as app:
        stage(app)
        preview = call(app, "dataDirectory.preview", target=str(destination))
        operation = str(uuid4())
        with monkeypatch.context() as patch:

            def unavailable(*_args: object) -> Never:
                raise OSError("disk disconnected")

            patch.setattr(data_migration, "snapshot", unavailable)
            failed = app.bridge.call(
                "dataDirectory.move",
                {"target": str(destination), "fingerprint": preview["fingerprint"], "operationId": operation},
            )
            assert not failed["ok"] and not selected.locator.exists()
        assert (
            app.writer.execute(
                lambda work: work.connection.execute("SELECT COUNT(*) FROM identity").fetchone()[0]
            )
            == 1
        )
        result = call(
            app,
            "dataDirectory.move",
            target=str(destination),
            fingerprint=preview["fingerprint"],
            operationId=operation,
        )
        assert result["restartRequired"]


@pytest.mark.parametrize("kind", ["nested", "nonempty", "override", "relative"])
def test_preflight_rejects_unsafe_destination(tmp_path: Path, kind: str) -> None:
    selected = location(tmp_path)
    if kind == "override":
        selected = DataLocation(selected.root, "environment", selected.locator)
    with Application(data_location=selected, start_scheduler=False) as app:
        target = selected.root / "nested" if kind == "nested" else tmp_path / "target"
        if kind == "nonempty":
            target.mkdir()
            (target / "unrelated.txt").write_text("keep", encoding="utf-8")
        response = app.bridge.call(
            "dataDirectory.preview", {"target": "relative" if kind == "relative" else str(target)}
        )
        assert not response["ok"]


def test_sqlite_snapshot_includes_committed_wal(tmp_path: Path) -> None:
    with Application(tmp_path / "data", start_scheduler=False) as app:
        stage(app)
        app.writer.execute(lambda work: work.connection.execute("PRAGMA wal_autocheckpoint=0"))
        expected = snapshot(app.data_dir / "htd.sqlite3", tmp_path / "copy.sqlite3")
        assert expected == fingerprint(app.data_dir / "htd.sqlite3")


@pytest.mark.parametrize("boundary", ["copying", "verified", "locator", "after_commit"])
def test_persistent_boundaries_keep_exactly_one_resumable_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    selected = location(tmp_path)
    destination = tmp_path / "target"
    original = data_migration.atomic_json
    with Application(data_location=selected, clock=FakeClock(AT), start_scheduler=False) as app:
        stage(app)
        preview = call(app, "dataDirectory.preview", target=str(destination))

        def interrupted(path: Path, value: dict[str, object]) -> None:
            if (
                (
                    boundary in {"copying", "verified"}
                    and path == destination / data_migration.JOURNAL
                    and value.get("phase") == boundary
                )
                or (boundary == "locator" and path == selected.locator)
                or (
                    boundary == "after_commit"
                    and path == destination / data_migration.JOURNAL
                    and value.get("phase") == "committed"
                )
            ):
                raise OSError("injected power interruption")
            original(path, value)

        with monkeypatch.context() as patch:
            patch.setattr(data_migration, "atomic_json", interrupted)
            response = app.bridge.call(
                "dataDirectory.move",
                {
                    "target": str(destination),
                    "fingerprint": preview["fingerprint"],
                    "operationId": str(uuid4()),
                },
            )
            assert not response["ok"]
        if boundary == "after_commit":
            assert app.directory_changed
            resolved = resolve_data_directory(environ={"LOCALAPPDATA": str(selected.locator.parent)})
            assert resolved.root == destination
        else:
            assert not app.directory_changed and not selected.locator.exists()
            assert (
                app.writer.execute(
                    lambda work: work.connection.execute("SELECT COUNT(*) FROM identity").fetchone()[0]
                )
                == 1
            )


def test_uninstall_cleanup_preserves_unrelated_files_and_selected_path(tmp_path: Path) -> None:
    selected = location(tmp_path)
    destination = tmp_path / "target"
    with Application(data_location=selected, start_scheduler=False) as app:
        preview = call(app, "dataDirectory.preview", target=str(destination))
        call(
            app,
            "dataDirectory.move",
            target=str(destination),
            fingerprint=preview["fingerprint"],
            operationId=str(uuid4()),
        )
    resolved = resolve_data_directory(environ={"LOCALAPPDATA": str(selected.locator.parent)})
    with Application(data_location=resolved, start_scheduler=False):
        pass
    unrelated = destination / "user-document.txt"
    unrelated.write_text("keep", encoding="utf-8")
    remove_application_data(resolved)
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert not (destination / "htd.sqlite3").exists()
    reopened = resolve_data_directory(environ={"LOCALAPPDATA": str(selected.locator.parent)})
    assert reopened.root == destination and reopened.phase == "deleted"
    with Application(data_location=reopened, start_scheduler=False) as app:
        assert app.data_dir == destination


def test_explicit_recovery_fences_unactivated_target(tmp_path: Path) -> None:
    selected = location(tmp_path)
    destination = tmp_path / "target"
    with Application(data_location=selected, start_scheduler=False) as app:
        preview = call(app, "dataDirectory.preview", target=str(destination))
        call(
            app,
            "dataDirectory.move",
            target=str(destination),
            fingerprint=preview["fingerprint"],
            operationId=str(uuid4()),
        )
    recover_unactivated_directory(selected.locator, selected.root)
    restored = resolve_data_directory(environ={"LOCALAPPDATA": str(selected.locator.parent)})
    assert restored.root == selected.root
    with Application(data_location=restored, start_scheduler=False):
        pass
    with pytest.raises(DataDirectoryError):
        Application(destination, start_scheduler=False)
    with pytest.raises(DataDirectoryError):
        recover_unactivated_directory(selected.locator, selected.root)


def test_cross_volume_copy_uses_snapshot_instead_of_directory_rename(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[3] / ".verify"
    workspace.mkdir(exist_ok=True)
    if tmp_path.drive.casefold() == workspace.drive.casefold():
        pytest.skip("A second local volume is unavailable on this machine")
    with tempfile.TemporaryDirectory(prefix="directory-cross-volume-", dir=workspace) as temporary:
        destination = Path(temporary) / "target"
        selected = location(tmp_path)
        with Application(data_location=selected, clock=FakeClock(AT), start_scheduler=False) as app:
            stage(app)
            expected = fingerprint(app.data_dir / "htd.sqlite3")
            preview = call(app, "dataDirectory.preview", target=str(destination))
            call(
                app,
                "dataDirectory.move",
                target=str(destination),
                fingerprint=preview["fingerprint"],
                operationId=str(uuid4()),
            )
        assert fingerprint(destination / "htd.sqlite3") == expected
