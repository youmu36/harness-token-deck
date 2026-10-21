"""Own the provider and asyncio client on a dedicated thread, never the scan thread."""

import asyncio
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from contextlib import suppress
from threading import Lock, Thread
from typing import Any

from htd.application.remote.service import Remote
from htd.domain.common.errors import DomainError


class BackgroundRemote:
    _loop: asyncio.AbstractEventLoop
    _stop: asyncio.Event
    _wake: asyncio.Event
    service: Remote

    def __init__(self, factory: Callable[[], Remote], *, automatic: bool = True) -> None:
        self._factory, self._automatic = factory, automatic
        self._ready: Future[None] = Future()
        self._lock = Lock()
        self._closed = False
        self._thread = Thread(target=self._run, name="htd-remote", daemon=True)
        self._thread.start()
        self._ready.result(10)

    def _run(self) -> None:
        async def main() -> None:
            self._loop = asyncio.get_running_loop()
            self.service = self._factory()
            self._stop = asyncio.Event()
            self._wake = asyncio.Event()
            self.service.wake = self._wake.set
            self._ready.set_result(None)
            while not self._stop.is_set():
                self._wake.clear()
                if self._automatic:
                    with suppress(Exception):
                        await self.service.tick()
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self.service.next_tick_delay() if self._automatic else 30
                    )
            await self.service.close()

        try:
            asyncio.run(main())
        except BaseException as error:
            if not self._ready.done():
                self._ready.set_exception(error)

    def call[T](self, action: Callable[[Remote], Coroutine[Any, Any, T]], *, wake: bool = False) -> T:
        async def run() -> T:
            try:
                return await action(self.service)
            finally:
                if wake:
                    self._wake.set()

        with self._lock:
            if self._closed:
                raise DomainError("APP_SHUTTING_DOWN", "应用正在关闭")
            future = asyncio.run_coroutine_threadsafe(run(), self._loop)
        return future.result(timeout=25)

    def invoke[T](self, action: Callable[[Remote], T], *, wake: bool = False) -> T:
        async def run(service: Remote) -> T:
            return action(service)

        return self.call(run, wake=wake)

    def suspend(self) -> None:
        self.call(lambda service: service.suspend())

    def resume(self) -> None:
        self.invoke(lambda service: setattr(service, "suspended", False), wake=True)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._loop.call_soon_threadsafe(self._stop.set)
            self._loop.call_soon_threadsafe(self._wake.set)
        self._thread.join(timeout=20)
        if self._thread.is_alive():
            raise DomainError("REMOTE_CLOSE_TIMEOUT", "远程刷新仍在关闭")
