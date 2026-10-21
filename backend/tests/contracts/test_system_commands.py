"""Desktop-only actions stay behind strict DTOs and the bridge's single public method."""

from pathlib import Path

from htd.bootstrap import Application


def test_system_actions_are_unavailable_headless_and_paths_are_whitelisted(tmp_path: Path) -> None:
    with Application(tmp_path / "data", start_scheduler=False) as app:
        for command, payload in (("app.pickPath", {"kind": "dir"}), ("app.openPath", {"kind": "dataDir"})):
            result = app.bridge.call(command, payload)
            assert result["ok"] is False
            assert isinstance(result["error"], dict)
            assert result["error"]["code"] == "DESKTOP_REQUIRED"
        assert app.bridge.call("app.openPath", {"kind": "C:/arbitrary"})["ok"] is False
        opened: list[str] = []
        app.system_actions.open = opened.append
        app.system_actions.pick = lambda kind, initial: str(tmp_path / "source") if kind == "dir" else None
        assert app.bridge.call("app.pickPath", {"kind": "dir", "initial": str(tmp_path)}) == {
            "ok": True,
            "data": {"path": str(tmp_path / "source")},
        }
        assert app.bridge.call("app.pickPath", {"kind": "file"}) == {"ok": True, "data": {"path": None}}
        assert app.bridge.call("app.openPath", {"kind": "logDir"}) == {"ok": True, "data": {"opened": True}}
        assert opened == ["logDir"]
