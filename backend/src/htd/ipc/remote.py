"""Remote commands hand work to the dedicated asyncio loop."""

from htd.application.remote.background import BackgroundRemote
from htd.ipc.dto.remote import (
    RemoteRequest,
    RemoteSettings,
    RemoteSnapshot,
    RemoteTestResponse,
    RemoteWrite,
    SaveRemoteRequest,
)
from htd.ipc.router import Router


def register_remote(router: Router, remote: BackgroundRemote) -> None:
    @router.register("remote.get", request=RemoteRequest, response=RemoteSnapshot, kind="read")
    def get(request: RemoteRequest) -> RemoteSnapshot:
        return RemoteSnapshot.model_validate(remote.invoke(lambda service: service.get(request.vendorId)))

    @router.register("remote.save", request=SaveRemoteRequest, response=RemoteSnapshot, kind="write")
    def save(request: SaveRemoteRequest) -> RemoteSnapshot:
        return RemoteSnapshot.model_validate(
            remote.invoke(
                lambda service: service.save(
                    request.model_dump(exclude={"operationId"}), request.operationId
                ),
                wake=True,
            )
        )

    @router.register(
        "remote.test", request=RemoteSettings, response=RemoteTestResponse, kind="read", timeout=25
    )
    def test(request: RemoteSettings) -> RemoteTestResponse:
        return RemoteTestResponse.model_validate(
            remote.call(lambda service: service.test(request.model_dump()))
        )

    @router.register("remote.refresh", request=RemoteWrite, response=RemoteSnapshot, kind="write", timeout=25)
    def refresh(request: RemoteWrite) -> RemoteSnapshot:
        return RemoteSnapshot.model_validate(
            remote.call(lambda service: service.refresh(request.vendorId, request.operationId), wake=True)
        )

    @router.register("remote.delete", request=RemoteWrite, response=RemoteSnapshot, kind="write")
    def delete(request: RemoteWrite) -> RemoteSnapshot:
        return RemoteSnapshot.model_validate(
            remote.invoke(lambda service: service.delete(request.vendorId, request.operationId), wake=True)
        )
