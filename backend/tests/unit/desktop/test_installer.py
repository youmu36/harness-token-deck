"""Backups used before destructive uninstall include WAL and never replace older backups."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from htd.adapters.sqlite.installer_backup import backup_data


def test_backup_includes_committed_wal_and_preserves_source(tmp_path: Path) -> None:
    source = tmp_path / "htd.sqlite3"
    destination = tmp_path / "desktop/backup.sqlite3"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE ledger (value TEXT)")
        connection.execute("INSERT INTO ledger VALUES ('committed WAL record')")
        connection.commit()
        assert source.with_suffix(".sqlite3-wal").stat().st_size > 0
        before = source.read_bytes()
        backup_data(tmp_path, destination)
        assert source.read_bytes() == before
        with closing(sqlite3.connect(destination)) as backup:
            assert backup.execute("SELECT value FROM ledger").fetchone() == ("committed WAL record",)
            assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_backup_failure_keeps_data_and_previous_backup(tmp_path: Path) -> None:
    source = tmp_path / "htd.sqlite3"
    source.write_bytes(b"invalid SQLite file")
    destination = tmp_path / "backup.sqlite3"
    destination.write_bytes(b"previous backup")
    with pytest.raises(sqlite3.DatabaseError):
        backup_data(tmp_path, destination)
    assert source.read_bytes() == b"invalid SQLite file"
    assert destination.read_bytes() == b"previous backup"
    assert not destination.with_name("backup.sqlite3.tmp").exists()


def test_existing_backup_is_never_overwritten(tmp_path: Path) -> None:
    with closing(sqlite3.connect(tmp_path / "htd.sqlite3")) as connection:
        connection.execute("CREATE TABLE ledger (value TEXT)")
    destination = tmp_path / "backup.sqlite3"
    destination.write_bytes(b"previous backup")
    with pytest.raises(FileExistsError):
        backup_data(tmp_path, destination)
    assert destination.read_bytes() == b"previous backup"
