"""Keep tests away from real application and source directories."""

from pathlib import Path

import pytest
from htd.domain.common.testing import FakeClock


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "USERPROFILE",
        "HOME",
        "APPDATA",
        "LOCALAPPDATA",
        "CODEX_HOME",
        "HTD_DATA_DIR",
        "HTD_SOURCE_ROOT_CODEX",
        "HTD_SOURCE_ROOT_ZCODE",
        "HTD_SOURCE_ROOT_DSH",
        "HTD_SOURCE_ROOT_CLAUDE",
        "CLAUDE_CONFIG_DIR",
    ):
        directory = tmp_path / name.lower()
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()
