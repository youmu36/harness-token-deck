"""Reuse the CLR evaluation thread for pywebview's short-lived API response workers."""

import threading
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any


@contextmanager
def evaluation_worker(window: Any) -> Generator[None]:
    original = window.evaluate_js
    gui_thread: int | None = None
    worker_thread: int | None = None

    def remember_gui() -> None:
        nonlocal gui_thread
        gui_thread = threading.get_ident()

    def invoke(*args: Any, **kwargs: Any) -> Any:
        nonlocal worker_thread
        worker_thread = threading.get_ident()
        return original(*args, **kwargs)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="htu-webview-evaluate") as executor:

        def evaluate(*args: Any, **kwargs: Any) -> Any:
            # GUI calls and reentrant callbacks must not wait for a worker that needs the GUI.
            if threading.get_ident() in {gui_thread, worker_thread}:
                return original(*args, **kwargs)
            return executor.submit(invoke, *args, **kwargs).result()

        window.events.before_show += remember_gui
        window.evaluate_js = evaluate
        try:
            yield
        finally:
            window.evaluate_js = original
            window.events.before_show -= remember_gui
