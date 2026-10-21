import json
from pathlib import Path
from typing import cast

import pytest
from htd.domain.sources.codex import CodexContext, parse_record

CODEX_FIXTURES = Path(__file__).resolve().parents[4] / "fixtures/codex"


@pytest.fixture
def codex_records() -> list[dict[str, object]]:
    return [
        cast(dict[str, object], json.loads(line))
        for line in (CODEX_FIXTURES / "desktop.jsonl").read_text(encoding="utf-8").splitlines()
    ]


@pytest.fixture
def codex_context(codex_records: list[dict[str, object]]) -> CodexContext:
    context = CodexContext()
    for record in codex_records[:2]:
        parse_record(record, context)
    return context


@pytest.fixture
def codex_request(codex_records: list[dict[str, object]]) -> dict[str, object]:
    return codex_records[2]
