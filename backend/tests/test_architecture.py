from pathlib import Path

import pytest

from scripts.check_architecture import check


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("domain/usage/parser.py", "import os"),
        ("domain/usage/parser.py", "import httpx"),
        ("domain/usage/parser.py", "from ...adapters import sqlite"),
        ("domain/usage/parser.py", "from htu.domain.pricing._private import rule"),
        ("application/service.py", "from htu.adapters import sqlite"),
        ("application/service.py", "import sqlite3"),
        ("adapters/sqlite/repository.py", "from htu.adapters.collectors import read"),
        ("adapters/desktop/window.py", "from htu.domain.billing import bill"),
        ("ipc/bridge.py", "from htu.adapters import collectors"),
        ("jobs/scheduler.py", "from htu.migration import run"),
        ("domain/common/money.py", "amount: list[float]"),
        ("domain/wallet/model.py", 'def total() -> "float": pass'),
    ],
)
def test_rejects_violations(tmp_path: Path, name: str, code: str) -> None:
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    path.write_text(code, encoding="utf-8")
    assert check(tmp_path)


def test_allows_pure_rules_and_type_only_adapters(tmp_path: Path) -> None:
    domain = tmp_path / "domain/usage"
    domain.mkdir(parents=True)
    (domain / "model.py").write_text("from dataclasses import dataclass\nfrom ..common import ids")
    ipc = tmp_path / "ipc"
    ipc.mkdir()
    (ipc / "bridge.py").write_text(
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from htu.adapters.desktop import Window"
    )
    assert check(tmp_path) == []
