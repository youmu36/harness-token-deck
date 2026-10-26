"""Streaming exact dataset fingerprints and WAL-aware directory-move snapshots."""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def is_native_dataset(path: Path) -> bool:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT value FROM meta WHERE key='app_id'").fetchone()
        return row == ("com.harness-token-deck.ledger",)


def fingerprint(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("Dataset integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Dataset foreign key check failed")
        result["__schema__"] = hashlib.sha256(
            repr(
                db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            ).encode()
        ).hexdigest()
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            quoted = '"' + name.replace('"', '""') + '"'
            digest = hashlib.sha256()
            try:
                rows = db.execute(f"SELECT * FROM {quoted} ORDER BY rowid")
            except sqlite3.OperationalError:
                columns = [row[1] for row in db.execute(f"PRAGMA table_info({quoted})")]
                order = ",".join('"' + str(key).replace('"', '""') + '"' for key in columns)
                rows = db.execute(f"SELECT * FROM {quoted} ORDER BY {order}")
            for row in rows:
                encoded = json.dumps(
                    [{"blob": value.hex()} if isinstance(value, bytes) else value for value in row],
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
            result[name] = digest.hexdigest()
    return result


def snapshot(source: Path, destination: Path) -> dict[str, str]:
    with destination.open("xb"):
        pass
    try:
        with (
            closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as reader,
            closing(sqlite3.connect(destination)) as writer,
        ):
            reader.backup(writer, pages=256, sleep=0.01)
        expected = fingerprint(source)
        if fingerprint(destination) != expected:
            raise RuntimeError("Directory move changed dataset facts or processing watermarks")
        return expected
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
