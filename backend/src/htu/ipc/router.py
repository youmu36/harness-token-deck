"""One strict command registry, shared by pywebview and JSON-line transports."""

import logging
import math
import re
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from threading import BoundedSemaphore, Condition, Lock
from types import MappingProxyType, TracebackType
from typing import Literal, cast

from pydantic import JsonValue, ValidationError

from htu.domain.common.errors import DomainError
from htu.ipc.dto.base import StrictModel, Success
from htu.ipc.errors import exception_envelope, invalid_request

type CommandKind = Literal["read", "write"]
_NAME = re.compile(r"[a-z][a-zA-Z0-9]*\.[a-z][a-zA-Z0-9]*")
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    request: type[StrictModel]
    response: type[StrictModel]
    kind: CommandKind
    timeout: float
    handler: Callable[[StrictModel], StrictModel]


class Router:
    def __init__(self, *, workers: int = 4, capacity: int = 64) -> None:
        self._commands: dict[str, Command] = {}
        self._lock = Lock()
        self._closed = False
        self._slots = BoundedSemaphore(capacity)
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="htu-ipc")
        self._gate = Condition()
        self._active = 0
        self._exclusive = False

    @property
    def commands(self) -> Mapping[str, Command]:
        with self._lock:
            return MappingProxyType(dict(self._commands))

    def register[Request: StrictModel, Response: StrictModel](
        self,
        name: str,
        *,
        request: type[Request],
        response: type[Response],
        kind: CommandKind,
        timeout: float = 30,
        exclusive: bool = False,
        after_success: Callable[[], None] | None = None,
    ) -> Callable[[Callable[[Request], Response]], Callable[[Request], Response]]:
        if _NAME.fullmatch(name) is None or kind not in ("read", "write"):
            raise ValueError("Invalid command name or kind")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Command timeout must be finite and positive")

        def decorate(handler: Callable[[Request], Response]) -> Callable[[Request], Response]:
            def invoke(value: StrictModel) -> StrictModel:
                with self._gate:
                    if self._exclusive:
                        raise DomainError("SERVER_BUSY", "迁移正在切换数据库，请稍后重试", retryable=True)
                    if exclusive:
                        self._exclusive = True
                        self._gate.notify_all()
                        while self._active:
                            self._gate.wait()
                    self._active += 1
                try:
                    result = handler(cast(Request, value))
                finally:
                    with self._gate:
                        self._active -= 1
                        if exclusive:
                            self._exclusive = False
                        self._gate.notify_all()
                if after_success is not None:
                    try:
                        after_success()
                    except Exception:
                        _LOG.exception("Post-command event delivery failed")
                return result

            with self._lock:
                if self._closed or name in self._commands:
                    raise ValueError("Router is closed or command is already registered")
                self._commands[name] = Command(name, request, response, kind, timeout, invoke)
            return handler

        return decorate

    def call(self, command: object, payload: object) -> dict[str, JsonValue]:
        try:
            if type(command) is not str:
                raise DomainError("INVALID_REQUEST", "命令名须为字符串", "command")
            with self._lock:
                if self._closed:
                    raise DomainError("APP_SHUTTING_DOWN", "应用正在关闭，请稍后重试")
                definition = self._commands.get(command)
            if definition is None:
                raise DomainError("UNKNOWN_COMMAND", "未知的应用命令", "command")
            if type(payload) is not dict:
                raise DomainError("INVALID_REQUEST", "参数须为 JSON 对象", "payload")
            try:
                value = definition.request.model_validate(payload)
            except ValidationError as error:
                return invalid_request(error)
            if not self._slots.acquire(blocking=False):
                raise DomainError("SERVER_BUSY", "应用正忙，请稍后重试")
            try:
                with self._lock:
                    if self._closed:
                        raise DomainError("APP_SHUTTING_DOWN", "应用正在关闭，请稍后重试")
                    future = self._executor.submit(definition.handler, value)
            except Exception:
                self._slots.release()
                raise
            future.add_done_callback(lambda _: self._slots.release())
            try:
                result = future.result(timeout=definition.timeout)
            except TimeoutError:
                if future.done():
                    result = future.result()  # A handler's own TimeoutError is an internal failure.
                else:
                    future.cancel()  # Running writes can still commit; reuse operationId on retry.
                    if definition.kind == "write":
                        raise DomainError("WRITE_TIMEOUT", "写入等待超时，请查询操作结果后重试") from None
                    raise DomainError("READ_TIMEOUT", "读取等待超时，请稍后重试") from None
            # Response failures are INTERNAL_ERROR, not client validation failures.
            if type(result) is not definition.response:
                raise TypeError("Command returned an incorrect response DTO")
            checked = definition.response.model_validate(result.model_dump(mode="python", warnings="error"))
            return Success(data=checked).model_dump(mode="json", exclude={"revisions"})
        except Exception as error:
            return exception_envelope(error)

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def __enter__(self) -> "Router":
        return self

    def __exit__(
        self, kind: type[BaseException] | None, value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()
