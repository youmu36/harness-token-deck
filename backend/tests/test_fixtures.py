import json
import sqlite3
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


@pytest.mark.parametrize("path", sorted([*FIXTURES.rglob("*.json"), *FIXTURES.rglob("*.jsonl")]))
def test_fixture_is_valid_json(path: Path) -> None:
    content = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        assert content.endswith("\n"), path
        for line in content.splitlines():
            assert isinstance(json.loads(line), dict), path
    else:
        assert isinstance(json.loads(content), (dict, list)), path


@pytest.mark.parametrize("native", [False, True])
def test_zcode_schema_materializes(tmp_path: Path, native: bool) -> None:
    name = "schema-native.sql" if native else "schema.sql"
    with sqlite3.connect(tmp_path / "fixture.sqlite3") as connection:
        connection.executescript((FIXTURES / "zcode" / name).read_text(encoding="utf-8"))
        cursor = connection.execute("SELECT rowid, * FROM model_usage LIMIT 0")
        columns = {str(column[0]) for column in cursor.description}
        assert {"rowid", "id", "session_id", "logical_request_id", "raw_usage_json"} <= columns
        assert ("input_tokens" in columns) is native
