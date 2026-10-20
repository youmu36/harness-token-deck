"""Copy synthetic old databases and mutate their configuration for isolated migration tests."""

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
AT = "2026-09-10T00:00:00.000Z"


def copy_legacy(tmp_path: Path, version: str = "v030") -> Path:
    path = tmp_path / (version + ".sqlite3")
    shutil.copyfile(FIXTURES / "legacy" / path.name, path)
    return path


def change_state(path: Path, table: str, change: Any) -> None:
    with closing(sqlite3.connect(path)) as db:
        data = json.loads(db.execute(f"SELECT state_json FROM {table}").fetchone()[0])
        change(data)
        db.execute(f"UPDATE {table} SET state_json=?", (json.dumps(data),))
        db.commit()
