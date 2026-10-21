"""Ordinary pywebview messages must not raise .NET string-conversion exceptions."""

# pyright: reportPrivateUsage=false

import json
from types import SimpleNamespace

import pytest
from htd.adapters.desktop.native import NativeWindow

LOCAL_URL = "http://127.0.0.1:12345/index.html"


class Message:
    def __init__(self, value: object, source: str = LOCAL_URL) -> None:
        self.Source = source
        self.WebMessageAsJson = json.dumps(value)
        self.value = value

    def TryGetWebMessageAsString(self) -> str:
        if not isinstance(self.value, str):
            raise ValueError("WebView2 rejects non-string messages")
        return self.value


def window(actions: list[str]) -> NativeWindow:
    native = object.__new__(NativeWindow)
    native.window = SimpleNamespace(real_url=LOCAL_URL)
    native.host_action = actions.append
    return native


@pytest.mark.parametrize("message", [["invoke", {}, "request-id"], {"result": True}, 0, None])
def test_normal_ipc_messages_are_ignored_without_warning(
    message: object, caplog: pytest.LogCaptureFixture
) -> None:
    actions: list[str] = []
    window(actions)._host_message(None, Message(message))
    assert actions == []
    assert caplog.records == []


@pytest.mark.parametrize("action", ["htd.openLogDir", "htd.quit", "htd.ready", "htd.unready"])
def test_local_control_strings_are_forwarded(action: str) -> None:
    actions: list[str] = []
    window(actions)._host_message(None, Message(action, LOCAL_URL + "#/settings"))
    assert actions == [action]


@pytest.mark.parametrize(
    "source", ["https://example.com/", "http://127.0.0.1:12346/index.html", LOCAL_URL + "?other"]
)
def test_foreign_documents_cannot_request_host_actions(source: str) -> None:
    actions: list[str] = []
    window(actions)._host_message(None, Message("htd.quit", source))
    assert actions == []


def test_unknown_strings_do_not_invoke_host_actions() -> None:
    actions: list[str] = []
    window(actions)._host_message(None, Message("htd.unregistered"))
    assert actions == []


@pytest.mark.parametrize("action", ["htd.openLogDir", "htd.quit", "htd.ready", "htd.unready"])
def test_control_messages_do_not_reach_pywebview_tuple_decoder(action: str) -> None:
    received: list[object] = []
    native = window([])

    def receive(sender: object, args: object) -> None:
        received.append(args)

    native.form = SimpleNamespace(browser=SimpleNamespace(on_script_notify=receive))
    native._script_message(None, Message(action))
    assert received == []


@pytest.mark.parametrize("message", [["call", "[]", "request-id"], "FilesDropped"])
def test_api_and_drag_messages_keep_the_pywebview_handler(message: object) -> None:
    received: list[object] = []
    native = window([])

    def receive(sender: object, args: object) -> None:
        received.append(args)

    native.form = SimpleNamespace(browser=SimpleNamespace(on_script_notify=receive))
    args = Message(message)
    native._script_message(None, args)
    assert received == [args]
