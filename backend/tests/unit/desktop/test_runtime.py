"""WebView2 preflight supports both registry scopes/views and rejects empty installations."""

import winreg

import pytest
from htd.adapters.desktop import runtime


def test_preflight_checks_user_machine_and_both_views(monkeypatch: pytest.MonkeyPatch) -> None:
    visited: list[tuple[int, int]] = []

    def registry(root: int, view: int) -> str | None:
        visited.append((root, view))
        return "154.0.0.0" if (root, view) == (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY) else None

    monkeypatch.setattr(runtime, "_registry_version", registry)
    assert runtime.webview2_version() == "154.0.0.0"
    assert visited == [
        (winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
    ]


@pytest.mark.parametrize(
    "value,expected", [("0.0.0.0", None), ("bad", None), (123, None), ("154.0.0.0", "154.0.0.0")]
)
def test_preflight_rejects_invalid_installed_version(
    monkeypatch: pytest.MonkeyPatch, value: object, expected: str | None
) -> None:
    class Key:
        def __enter__(self) -> "Key":
            return self

        def __exit__(self, *args: object) -> None:
            pass

    def open_key(*args: object) -> Key:
        return Key()

    def query(*args: object) -> tuple[object, int]:
        return value, winreg.REG_SZ

    monkeypatch.setattr(winreg, "OpenKey", open_key)
    monkeypatch.setattr(winreg, "QueryValueEx", query)
    assert runtime.webview2_version() == expected
