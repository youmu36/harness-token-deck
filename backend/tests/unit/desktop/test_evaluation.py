"""Transient API workers reuse one evaluation thread while GUI/reentrant calls stay direct."""

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from htd.adapters.desktop.evaluation import evaluation_worker


class Event:
    def __init__(self) -> None:
        self.items: list[Any] = []

    def __iadd__(self, item: Any) -> "Event":
        self.items.append(item)
        return self

    def __isub__(self, item: Any) -> "Event":
        self.items.remove(item)
        return self


class Window:
    def __init__(self) -> None:
        self.events: Any = self
        self.before_show = Event()
        self.ids: list[int] = []

    def evaluate_js(self, script: str, *, callback: object = None) -> str:
        self.ids.append(threading.get_ident())
        if script == "fail":
            raise ValueError("synthetic evaluation failure")
        if script == "reentrant":
            return self.evaluate_js("nested")
        return script + str(callback or "")


def test_worker_reused_results_errors_and_reentrant_calls_preserved() -> None:
    window: Any = Window()
    original = window.evaluate_js
    with evaluation_worker(window), ThreadPoolExecutor(max_workers=4) as callers:
        requests = [callers.submit(window.evaluate_js, str(index), callback="ok") for index in range(8)]
        assert [request.result(timeout=2) for request in requests] == [f"{index}ok" for index in range(8)]
        assert len(set(window.ids)) == 1
        with pytest.raises(ValueError, match="synthetic"):
            window.evaluate_js("fail")
        assert window.evaluate_js("reentrant") == "nested"
    assert window.evaluate_js == original
    assert window.before_show.items == []
    assert not any(thread.name.startswith("htd-webview-evaluate") for thread in threading.enumerate())


def test_gui_thread_is_not_dispatched_to_avoid_invoke_deadlock() -> None:
    window: Any = Window()
    with evaluation_worker(window):
        window.before_show.items[0]()
        assert window.evaluate_js("gui") == "gui"
        assert window.ids == [threading.get_ident()]
