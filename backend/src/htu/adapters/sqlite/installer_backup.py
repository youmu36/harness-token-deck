"""Consistent backup before installer data removal; never overwrite an older backup."""

import sqlite3
from contextlib import closing
from pathlib import Path


def backup_data(directory: Path, destination: Path) -> None:
    source = directory / "htu.sqlite3"
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    with temporary.open("xb"):
        pass
    try:
        with (
            closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as reader,
            closing(sqlite3.connect(temporary)) as writer,
        ):
            reader.backup(writer)
            if writer.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("Backup integrity check failed")
        temporary.rename(destination)
    finally:
        temporary.unlink(missing_ok=True)
