from pathlib import Path
from urllib.parse import quote

import pytest

from scripts.check_docs import check


@pytest.mark.parametrize("ticks", ["`", "``"])
def test_link_checker_ignores_inline_code_but_checks_real_links(tmp_path: Path, ticks: str) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text(
        f"Syntax: {ticks}HH:MM:SS[.fraction](Z/offset){ticks}\n[Missing document](docs/missing.md)\n",
        encoding="utf-8",
    )
    errors, warnings = check(tmp_path)
    assert errors == ["README.md: missing link docs/missing.md"]
    assert warnings == []


def test_link_checker_validates_local_and_encoded_duplicate_heading_anchors(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text(
        f"[Money](docs/money.md#{quote('金额与-pico')})\n"
        "[Duplicate](docs/money.md#金额与-pico-1)\n"
        "[Removed heading](docs/money.md#old)\n"
        "# Entry\n[Same document](#entry)\n[Missing local](#gone)\n",
        encoding="utf-8",
    )
    (tmp_path / "docs/money.md").write_text("# 金额与 `pico`\n# 金额与 `pico`\n", encoding="utf-8")
    errors, warnings = check(tmp_path)
    assert errors == [
        "README.md: missing anchor docs/money.md#old",
        "README.md: missing anchor #gone",
    ]
    assert warnings == []


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_fenced_examples_are_not_links_or_heading_targets(tmp_path: Path, fence: str) -> None:
    (tmp_path / "README.md").write_text(
        f'{fence}md\n# Fake\n<a id="example"></a>\n[Ignored](missing.md)\n{fence}\n'
        "[Fake](#fake)\n[Fake HTML](#example)\n",
        encoding="utf-8",
    )
    errors, warnings = check(tmp_path)
    assert errors == ["README.md: missing anchor #fake", "README.md: missing anchor #example"]
    assert warnings == []


def test_link_checker_includes_related_probe_readme(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Project\n", encoding="utf-8")
    directory = tmp_path / "scripts/probes"
    directory.mkdir(parents=True)
    (directory / "README.md").write_text("[Removed guide](../../docs/old.md)\n", encoding="utf-8")
    errors, warnings = check(tmp_path)
    assert errors == ["scripts/probes/README.md: missing link ../../docs/old.md"]
    assert warnings == []


def test_navigation_reports_orphans_and_handles_cycles(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Project\n", encoding="utf-8")
    directory = tmp_path / "docs"
    directory.mkdir()
    (directory / "README.md").write_text("[Module](module.md)\n", encoding="utf-8")
    (directory / "module.md").write_text("[Entry](README.md)\n", encoding="utf-8")
    (directory / "orphan.md").write_text("# Forgotten\n", encoding="utf-8")
    errors, warnings = check(tmp_path)
    assert errors == []
    assert warnings == ["docs/orphan.md is not reachable from docs/README.md"]
