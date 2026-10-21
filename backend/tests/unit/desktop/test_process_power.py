"""Optional resident-page reclamation must close its temporary process handle."""

from typing import Any

import pytest
from htd.adapters.desktop import process_power


@pytest.mark.parametrize("opened,succeeds", [(True, True), (True, False), (False, False)])
def test_reclaim_closes_quota_handle_even_when_windows_rejects_reclamation(
    monkeypatch: pytest.MonkeyPatch, opened: bool, succeeds: bool
) -> None:
    closed: list[int] = []

    def function(library: str, name: str, arguments: object, result: object) -> Any:
        if name == "OpenProcess":

            def open_process(*args: object) -> int:
                return 42 if opened else 0

            return open_process
        assert name == "K32EmptyWorkingSet"

        def empty(*args: object) -> bool:
            return succeeds

        return empty

    monkeypatch.setattr(process_power, "native_function", function)
    monkeypatch.setattr(process_power, "close_handle", closed.append)
    process = process_power.BackgroundProcess.__new__(process_power.BackgroundProcess)
    process.pid = 123
    if opened and succeeds:
        process.reclaim()
    else:
        with pytest.raises(OSError):
            process.reclaim()
    assert closed == ([42] if opened else [])
