"""Stage verification runs the Python suite once at the requested scope."""

import pytest

from scripts.verify import steps


@pytest.mark.parametrize(
    "level,marker",
    [
        ("quick", "not slow and not desktop"),
        ("full", "not desktop"),
        ("desktop", "not desktop"),
    ],
)
def test_verification_runs_each_python_scope_once(level: str, marker: str) -> None:
    commands = [step.command for step in steps(level) if "pytest" in step.command]
    assert [command[-2:] for command in commands] == [
        ("-m", marker),
        *([("-m", "desktop")] if level == "desktop" else []),
    ]


def test_desktop_verification_runs_functional_tests_after_frontend_build() -> None:
    desktop = steps("desktop")
    names = [step.name for step in desktop]
    assert names.index("Production frontend") < names.index("Desktop native tests")
    assert "Desktop lifecycle loop" not in names
    assert not any(step.skip for step in desktop)


def test_release_only_checks_frontend_functionality_then_builds_once() -> None:
    release = steps("release")
    names = [step.name for step in release]
    assert names == ["vue-tsc", "vitest", "Production frontend", "Release build, frozen smoke and NSIS"]
    build = next(step for step in release if "build_release.py" in " ".join(step.command))
    assert build.command[-1] == "--from-verifier"
    assert not any(
        "lifecycle_loop.py" in str(step.command) or "verify_installer.py" in str(step.command)
        for step in release
    )
    assert not any(step.skip for step in release)


def test_ci_checks_publication_then_functionality_and_build_without_packaging() -> None:
    ci = steps("ci")
    assert ci[0].command[-2:] == ("scripts/check_publication.py", "--history")
    assert [step.command[-2:] for step in ci if "pytest" in step.command] == [
        ("-m", "not slow and not desktop")
    ]
    assert [step.command[-1] for step in ci[2:]] == ["typecheck", "test", "build"]
    assert not any("build_release.py" in " ".join(step.command) for step in ci)
