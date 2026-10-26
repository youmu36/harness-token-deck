"""Register handshake, recovery and desktop system-action contracts."""

from collections.abc import Callable

from htd.domain.common.errors import DomainError
from htd.ipc.dto.app import (
    HelloRequest,
    HelloResponse,
    OpenPathRequest,
    OpenPathResponse,
    PickPathRequest,
    PickPathResponse,
    SyncRequest,
    SyncResponse,
)
from htd.ipc.dto.base import Revisions
from htd.ipc.events import EventForwarder
from htd.ipc.router import Router


class SystemActions:
    """Desktop installs GUI callbacks; headless commands return an explicit error."""

    pick: Callable[[str, str | None], str | None] | None = None
    open: Callable[[str], None] | None = None
    restart: Callable[[], None] | None = None

    def unavailable(self) -> None:
        raise DomainError("DESKTOP_REQUIRED", "此操作需要在桌面应用中执行")


def register_app_commands(
    router: Router,
    *,
    app_version: str,
    data_dir: str,
    data_dir_source: str = "default",
    revisions: Callable[[], Revisions],
    events: EventForwarder,
    system_actions: SystemActions | None = None,
) -> None:
    actions = system_actions or SystemActions()

    @router.register(
        "app.pickPath", request=PickPathRequest, response=PickPathResponse, kind="read", timeout=600
    )
    def pick_path(request: PickPathRequest) -> PickPathResponse:
        if actions.pick is None:
            actions.unavailable()
        assert actions.pick is not None
        return PickPathResponse(path=actions.pick(request.kind, request.initial))

    @router.register("app.openPath", request=OpenPathRequest, response=OpenPathResponse, kind="read")
    def open_path(request: OpenPathRequest) -> OpenPathResponse:
        if actions.open is None:
            actions.unavailable()
        assert actions.open is not None
        actions.open(request.kind)
        return OpenPathResponse(opened=True)

    @router.register("app.hello", request=HelloRequest, response=HelloResponse, kind="read")
    def hello(request: HelloRequest) -> HelloResponse:
        if request.clientProtocol != 1:
            raise DomainError(
                "PROTOCOL_MISMATCH", "客户端与应用协议版本不兼容，请更新后重试", "clientProtocol"
            )
        return HelloResponse(
            appVersion=app_version,
            capabilities=sorted(router.commands),
            dataDir=data_dir,
            dataDirSource=data_dir_source,
            revisions=revisions(),
        )

    @router.register("app.sync", request=SyncRequest, response=SyncResponse, kind="read")
    def sync(request: SyncRequest) -> SyncResponse:
        return SyncResponse(revisions=revisions(), pendingEvents=events.snapshot())
