"""Named mutex plus authenticated, bounded JSON command/ACK transport; no database imports."""

import hashlib
import json
import logging
import os
import re
import threading
import time
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener, answer_challenge, deliver_challenge
from pathlib import Path
from types import TracebackType
from typing import Any, cast

from htd.adapters.desktop._win32 import (
    NamedMutex,
    allow_pipe_foreground,
    close_handle,
    current_user_sid,
    pipe_process,
    wait_process,
    wake_pipe,
)
from htd.adapters.desktop.credentials import Credentials, read_key

_LOG = logging.getLogger(__name__)


class _DeadlineConnection:
    """Bound the stdlib's mutual-authentication reads, including silent peers."""

    def __init__(self, connection: Any, deadline: float, stopped: threading.Event) -> None:
        self.connection, self.deadline, self.stopped = connection, deadline, stopped

    def send_bytes(self, data: bytes) -> None:
        if self.stopped.is_set() or time.monotonic() >= self.deadline:
            raise TimeoutError("单实例通信超时")
        self.connection.send_bytes(data)

    def recv_bytes(self, maxlength: int = 4096) -> bytes:
        while not self.stopped.is_set():
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("单实例通信超时")
            if self.connection.poll(min(remaining, 0.05)):
                return self.connection.recv_bytes(maxlength)
        raise ConnectionAbortedError("单实例通信已停止")


class SingleInstance:
    def __init__(
        self,
        directory: Path,
        *,
        name: str = "HarnessTokenDeck.Instance",
        pipe_name: str = "HarnessTokenDeck",
    ) -> None:
        if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", value) for value in (name, pipe_name)):
            raise ValueError("无效的单实例名称")
        self.directory = directory.resolve()
        self.sid = current_user_sid()
        self.user_hash = hashlib.sha256(self.sid.encode("utf-8")).hexdigest()[:32]
        self.address = rf"\\.\pipe\{pipe_name}-{self.user_hash}"
        self._mutex = NamedMutex(rf"Local\{name}")
        self.primary = not self._mutex.exists
        self._test_hooks = os.environ.get("HTD_TEST_HOOKS") == "1"
        self._listener: Listener | None = None
        self._credentials: Credentials | None = None
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()
        self.quitting = threading.Event()
        self._condition = threading.Condition()
        self._pending: set[str] = set()

    def listen(self) -> None:
        if not self.primary or self._listener is not None:
            raise RuntimeError("只有第一实例可以开始监听")
        credentials = Credentials(self.directory, self.user_hash, self.sid)
        try:
            # Authenticate after accept so each challenge read has a deadline.
            listener = Listener(self.address, family="AF_PIPE", authkey=None)
        except BaseException:
            credentials.close()
            raise
        self._credentials, self._listener = credentials, listener
        self._thread = threading.Thread(target=self._serve, name="htd-instance-pipe", daemon=True)
        self._thread.start()
        credentials.publish()

    def _queue(self, command: object) -> bool:
        allowed = {"activate", "quit"}
        if self._test_hooks:
            allowed.add("test.trayOpen")
        if not isinstance(command, dict):
            return False
        fields = cast(dict[str, object], command)
        if set(fields) != {"cmd"}:
            return False
        name = fields["cmd"]
        if not isinstance(name, str) or name not in allowed:
            return False
        with self._condition:
            if self._stopped.is_set():
                return False
            if name == "quit":
                self.quitting.set()
            self._pending.add(name)
            self._condition.notify()
        return True

    def next_command(self, timeout: float = 0.1) -> str | None:
        with self._condition:
            self._condition.wait_for(lambda: bool(self._pending) or self._stopped.is_set(), timeout)
            if not self._pending:
                return None
            command = "quit" if "quit" in self._pending else sorted(self._pending)[0]
            self._pending.remove(command)
            return command

    def _serve(self) -> None:
        assert self._listener is not None and self._credentials is not None
        try:
            while not self._stopped.is_set():
                try:
                    with self._listener.accept() as connection:
                        channel: Any = _DeadlineConnection(connection, time.monotonic() + 3, self._stopped)
                        deliver_challenge(channel, self._credentials.key)
                        answer_challenge(channel, self._credentials.key)
                        command = json.loads(channel.recv_bytes())
                        channel.send_bytes(json.dumps({"ok": self._queue(command)}).encode("utf-8"))
                except (OSError, EOFError, AuthenticationError, ValueError):
                    if not self._stopped.is_set():
                        _LOG.warning("单实例管道请求未通过认证、校验或响应时限")
        finally:
            self._listener.close()

    def forward(
        self, command: str = "activate", *, timeout: float = 3.0, wait_for_exit: bool = False
    ) -> bool:
        deadline, stopped = time.monotonic() + timeout, threading.Event()
        completed = threading.Event()
        acknowledged = False

        def exchange() -> None:
            nonlocal acknowledged
            try:
                while not stopped.is_set() and time.monotonic() < deadline:
                    try:
                        key = read_key(self.directory, self.user_hash)
                        with Client(self.address, family="AF_PIPE", authkey=None) as connection:
                            channel: Any = _DeadlineConnection(connection, deadline, stopped)
                            answer_challenge(channel, key)
                            deliver_challenge(channel, key)
                            if command in {"activate", "test.trayOpen"}:
                                allow_pipe_foreground(connection.fileno())
                            handle = (
                                pipe_process(connection.fileno())
                                if wait_for_exit and command == "quit"
                                else None
                            )
                            try:
                                channel.send_bytes(json.dumps({"cmd": command}).encode("utf-8"))
                                acknowledged = json.loads(channel.recv_bytes()) == {"ok": True}
                                if acknowledged and handle is not None:
                                    acknowledged = wait_process(handle, 10)
                            finally:
                                if handle is not None:
                                    close_handle(handle)
                            return
                    except (OSError, EOFError):
                        stopped.wait(min(0.025, max(0, deadline - time.monotonic())))
            except (AuthenticationError, ValueError):
                pass
            finally:
                completed.set()

        # PipeClient has its own 20s busy timeout; never let it extend the secondary's 3s deadline.
        worker = threading.Thread(target=exchange, name="htd-instance-client", daemon=True)
        worker.start()
        completed.wait(max(0, deadline - time.monotonic()) + (10 if wait_for_exit else 0))
        stopped.set()
        return completed.is_set() and acknowledged

    def close(self) -> None:
        self._stopped.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread is not None and self._thread.is_alive():
            wake_pipe(self.address)
            self._thread.join(timeout=1)
            if self._thread.is_alive():
                raise TimeoutError("单实例管道未能停止")
        if self._credentials is not None:
            self._credentials.close()
        self._mutex.close()

    def __enter__(self) -> "SingleInstance":
        return self

    def __exit__(
        self, kind: type[BaseException] | None, value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()
