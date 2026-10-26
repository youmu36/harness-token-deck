"""Load the HTTP provider only when an account actually requests a balance."""

from htd.domain.common.time import Clock
from htd.domain.remote.model import BalanceObservation, RemoteAccount
from htd.domain.remote.ports import BalanceProvider


class LazyNewApiProvider:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._provider: BalanceProvider | None = None
        self._closed = False

    async def fetch(self, account: RemoteAccount, credential: str) -> BalanceObservation:
        if self._closed:
            raise RuntimeError("Remote provider is closed")
        if self._provider is None:
            from htd.adapters.providers.newapi import NewApiProvider

            self._provider = NewApiProvider(self._clock)
        return await self._provider.fetch(account, credential)

    async def aclose(self) -> None:
        self._closed = True
        if self._provider is not None:
            await self._provider.aclose()
            self._provider = None
