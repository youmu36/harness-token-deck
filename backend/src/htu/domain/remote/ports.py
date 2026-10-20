"""Network access lives in providers and runs on a background event loop."""

from typing import Protocol

from htu.domain.remote.model import BalanceObservation, RemoteAccount


class BalanceProvider(Protocol):
    async def fetch(self, account: RemoteAccount, credential: str) -> BalanceObservation: ...

    async def aclose(self) -> None: ...
