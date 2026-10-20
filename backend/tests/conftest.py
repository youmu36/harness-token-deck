"""Keep tests away from real application and source directories."""

from pathlib import Path

import pytest
from htu.domain.common.testing import FakeClock


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "USERPROFILE",
        "HOME",
        "APPDATA",
        "LOCALAPPDATA",
        "CODEX_HOME",
        "HTU_DATA_DIR",
        "HTU_SOURCE_ROOT_CODEX",
        "HTU_SOURCE_ROOT_ZCODE",
        "HTU_SOURCE_ROOT_DSH",
        "HTU_SOURCE_ROOT_CLAUDE",
        "CLAUDE_CONFIG_DIR",
    ):
        directory = tmp_path / name.lower()
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()
