"""Fake-clock scheduling, merged requests, cancellation and persisted metadata."""

import asyncio
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from htd.adapters.sqlite.account_book import AccountBook
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.remote_store import SqliteRemoteStore
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.adapters.sqlite.writer import WriteExecutor
from htd.application.remote.ports import MemoryCredentialStore
from htd.application.remote.service import Remote
from htd.domain.common.errors import DomainError
from htd.domain.common.testing import FakeClock
from htd.domain.remote.model import BalanceKind, BalanceObservation, RemoteAccount, RemoteFailure

from backend.tests.integration.test_wallet_storage import vendor


class Provider:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls = 0
        self.error: RemoteFailure | None = None
        self.gate: asyncio.Event | None = None
        self.cancelled = False
        self.closed = False
        self.kind = BalanceKind.ACCOUNT_REMAINING
        self.uncertain = False

    async def fetch(self, account: RemoteAccount, credential: str) -> BalanceObservation:
        self.calls += 1
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return BalanceObservation(
            account.id,
            self.clock.now_utc(),
            self.kind,
            "example.test",
            "USD",
            None
            if self.error or self.kind in (BalanceKind.UNLIMITED, BalanceKind.QUOTA_UNITS)
            else 1_975_310_000_001,
            raw_quota=987655,
            quota_per_unit=500000,
            currency_uncertain=self.uncertain,
            display_in_currency=True,
            error=self.error,
        )

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def remote(tmp_path: Path) -> Generator[Remote, None, None]:
    with WriteExecutor(tmp_path / "htd.sqlite3") as writer:
        writer.execute(vendor)
        with ReadPool(writer.path) as readers:
            clock = FakeClock()
            service = Remote(
                SqliteRemoteStore(writer, readers), MemoryCredentialStore(), Provider(clock), clock
            )
            yield service
            asyncio.run(service.close())


def settings(**changes: object) -> dict[str, object]:
    return {
        "vendorId": "vendor",
        "baseUrl": "https://example.test",
        "siteType": "newapi",
        "authMode": "access_token",
        "userId": "42",
        "credential": "synthetic-credential-1234",
        **changes,
    }


async def settle(service: Remote) -> None:
    await service.tick()
    tasks = [s.task for s in service.states.values() if s.task]
    await asyncio.gather(*tasks)


def test_automatic_refresh_topups_are_exact_and_not_repeated(remote: Remote) -> None:
    remote.save(settings(), str(uuid4()))
    asyncio.run(settle(remote))
    assert isinstance(remote.store, SqliteRemoteStore)
    assert isinstance(remote.clock, FakeClock)
    remote.clock.advance(300_000)
    asyncio.run(settle(remote))
    totals = remote.store.writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
    assert (totals.r, totals.a, totals.b) == (0, 0, 0)
    account = remote.store.writer.execute(lambda w: AccountBook(w.connection).summary("vendor"))
    assert account["B"] == "1975310000001" and account["R"] == "0"
    rows = remote.store.writer.execute(
        lambda w: w.connection.execute("SELECT COUNT(*) FROM wallet_entry WHERE kind='top_up'").fetchone()
    )
    assert rows[0] == 0


def test_deadline_wait_has_no_per_second_idle_poll_and_preserves_refresh_due(remote: Remote) -> None:
    assert remote.next_tick_delay() == 30
    remote.save(settings(), str(uuid4()))
    assert remote.next_tick_delay() == 0.05
    asyncio.run(settle(remote))
    assert remote.next_tick_delay() == 30
    assert isinstance(remote.clock, FakeClock)
    remote.clock.advance(299_000)
    assert remote.next_tick_delay() == 1
    remote.clock.advance(1000)
    assert remote.next_tick_delay() == 0.05


@pytest.mark.parametrize("enabled", [True, False])
def test_failed_sync_retries_after_ten_seconds_at_most_twice_then_returns_to_normal_schedule(
    remote: Remote, enabled: bool
) -> None:
    assert isinstance(remote.clock, FakeClock) and isinstance(remote.provider, Provider)
    clock, provider = remote.clock, remote.provider
    remote.save(settings(enabled=enabled), str(uuid4()))
    provider.error = RemoteFailure("timeout", "暂时超时")
    wakes: list[int] = []
    remote.wake = lambda: wakes.append(provider.calls)
    asyncio.run(remote.refresh("vendor"))
    for attempt in (2, 3):
        assert remote.next_tick_delay() == 10
        clock.advance(9999)
        asyncio.run(settle(remote))
        assert provider.calls == attempt - 1
        clock.advance(1)
        asyncio.run(settle(remote))
        assert provider.calls == attempt
    assert wakes == [1, 2, 3]
    assert next(iter(remote.states.values())).retry_attempt == 0
    clock.advance(10_000)
    asyncio.run(settle(remote))
    assert provider.calls == 3
    clock.advance(290_000)
    asyncio.run(settle(remote))
    assert provider.calls == (4 if enabled else 3)


def test_success_cancels_retry_and_preserves_last_success_during_failures(remote: Remote) -> None:
    assert isinstance(remote.clock, FakeClock) and isinstance(remote.provider, Provider)
    remote.save(settings(), str(uuid4()))
    asyncio.run(remote.refresh("vendor"))
    original = remote.get("vendor")["observation"]
    remote.provider.error = RemoteFailure("offline", "暂时离线")
    asyncio.run(remote.refresh("vendor"))
    assert remote.get("vendor")["observation"] is not None and original is not None
    remote.clock.advance(10_000)
    remote.provider.error = None
    asyncio.run(settle(remote))
    state = next(iter(remote.states.values()))
    assert state.retry_attempt == 0 and state.due - remote.clock.monotonic() == 300
    remote.clock.advance(10_000)
    asyncio.run(settle(remote))
    assert remote.provider.calls == 3


@pytest.mark.parametrize("minutes", [1, 2, 3, 4])
def test_short_refresh_intervals_persist_but_never_use_the_ten_second_retry_chain(
    remote: Remote, minutes: int
) -> None:
    assert isinstance(remote.clock, FakeClock) and isinstance(remote.provider, Provider)
    remote.save(settings(refreshMinutes=minutes), str(uuid4()))
    remote.provider.error = RemoteFailure("timeout", "暂时超时")
    asyncio.run(remote.refresh("vendor"))
    state = next(iter(remote.states.values()))
    assert state.account.refresh_minutes == minutes and state.retry_attempt == 0
    assert state.due - remote.clock.monotonic() == minutes * 60
    remote.states.clear()  # Recover from the persisted failure without creating a retry.
    remote.clock.advance(10_000)
    asyncio.run(settle(remote))
    assert remote.provider.calls == 1 and next(iter(remote.states.values())).retry_attempt == 0
    remote.clock.advance(minutes * 60_000 - 10_000)
    asyncio.run(settle(remote))
    assert remote.provider.calls == 2


def test_changing_to_one_minute_cancels_an_existing_retry(remote: Remote) -> None:
    assert isinstance(remote.clock, FakeClock) and isinstance(remote.provider, Provider)
    remote.save(settings(), str(uuid4()))
    remote.provider.error = RemoteFailure("offline", "暂时离线")
    asyncio.run(remote.refresh("vendor"))
    assert next(iter(remote.states.values())).retry_attempt == 1
    remote.save(settings(refreshMinutes=1, credential=None), str(uuid4()))
    assert next(iter(remote.states.values())).retry_attempt == 0
    remote.clock.advance(10_000)
    asyncio.run(settle(remote))
    assert remote.provider.calls == 1


def test_deleting_account_cancels_scheduled_retry(remote: Remote) -> None:
    assert isinstance(remote.clock, FakeClock) and isinstance(remote.provider, Provider)
    remote.save(settings(), str(uuid4()))
    remote.provider.error = RemoteFailure("offline", "暂时离线")
    asyncio.run(remote.refresh("vendor"))
    remote.delete("vendor", str(uuid4()))
    remote.clock.advance(10_000)
    asyncio.run(settle(remote))
    assert remote.provider.calls == 1


def test_backoff_sequence_manual_gate_and_retry_after(remote: Remote) -> None:
    provider = remote.provider
    assert isinstance(provider, Provider) and isinstance(remote.clock, FakeClock)
    clock = remote.clock
    remote.save(settings(), str(uuid4()))
    provider.error = RemoteFailure("rate_limited", "限流", 429)

    async def scenario() -> None:
        for delay in (60, 120, 240, 480, 960, 1800, 1800):
            await settle(remote)
            state = next(iter(remote.states.values()))
            assert state.due - remote.clock.monotonic() == delay
            calls = provider.calls
            await remote.refresh("vendor")
            assert provider.calls == calls
            clock.advance(delay * 1000)
        provider.error = RemoteFailure("rate_limited", "等待", 429, 90)
        await settle(remote)
        assert next(iter(remote.states.values())).due - remote.clock.monotonic() == 90
        clock.advance(90_000)
        provider.error = None
        await settle(remote)
        assert next(iter(remote.states.values())).failures == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_survives_restart_and_requires_new_credential(remote: Remote, status: int) -> None:
    provider = remote.provider
    assert isinstance(provider, Provider) and isinstance(remote.clock, FakeClock)
    remote.save(settings(), str(uuid4()))
    provider.error = RemoteFailure("auth_failed", "更新令牌", status)
    asyncio.run(settle(remote))
    remote.states.clear()
    remote.clock.advance(3_600_000)
    asyncio.run(settle(remote))
    assert provider.calls == 1
    remote.save(settings(credential=None, refreshMinutes=10), str(uuid4()))
    asyncio.run(settle(remote))
    assert provider.calls == 1
    provider.error = None
    operation = str(uuid4())
    remote.save(settings(credential="replacement-5678"), operation)
    asyncio.run(settle(remote))
    assert provider.calls == 2 and remote.get("vendor")["status"] == "ok"
    remote.save(settings(credential="replacement-5678"), operation)
    asyncio.run(settle(remote))
    assert provider.calls == 2


def test_failed_refresh_preserves_success_stale_and_metadata(remote: Remote) -> None:
    assert isinstance(remote.provider, Provider) and isinstance(remote.clock, FakeClock)
    remote.save(settings(enabled=False), str(uuid4()))
    asyncio.run(remote.refresh("vendor"))
    original = remote.get("vendor")["observation"]
    assert isinstance(original, dict) and original["canCalibrate"] and original["displayInCurrency"]
    remote.provider.error = RemoteFailure("offline", "无法连接")
    remote.clock.advance(600_001)
    asyncio.run(remote.refresh("vendor"))
    snapshot = remote.get("vendor")
    observation = snapshot["observation"]
    assert isinstance(observation, dict) and observation["amountPico"] == original["amountPico"]
    assert observation["stale"] and not observation["canCalibrate"] and snapshot["status"] == "offline"
    history = remote.store.observations(next(iter(remote.states)))
    assert history[1].quota_per_unit == 500000 and history[1].currency == "USD"
    assert history[1].source == "example.test" and not history[1].currency_uncertain


def test_manual_success_does_not_unlock_auth_suspension_without_new_credentials(remote: Remote) -> None:
    assert isinstance(remote.provider, Provider) and isinstance(remote.clock, FakeClock)
    provider, clock = remote.provider, remote.clock
    remote.save(settings(), str(uuid4()))
    provider.error = RemoteFailure("auth_failed", "更新令牌", 401)
    asyncio.run(remote.refresh("vendor"))
    provider.error = None
    asyncio.run(remote.refresh("vendor"))
    remote.states.clear()
    clock.advance(600_000)
    asyncio.run(settle(remote))
    assert provider.calls == 2 and remote.get("vendor")["status"] == "auth_failed"


def test_same_account_requests_merge_and_delete_cancels_without_late_observation(remote: Remote) -> None:
    assert isinstance(remote.provider, Provider)
    provider = remote.provider
    remote.save(settings(enabled=False), str(uuid4()))

    async def scenario() -> None:
        provider.gate = asyncio.Event()
        first = asyncio.create_task(remote.refresh("vendor", str(uuid4())))
        second = asyncio.create_task(remote.refresh("vendor", str(uuid4())))
        for _ in range(5):
            await asyncio.sleep(0)
        assert provider.calls == 1
        provider.gate.set()
        await asyncio.gather(first, second)
        assert len(remote.store.observations(next(iter(remote.states)))) == 1
        provider.gate = asyncio.Event()
        task = asyncio.create_task(remote.refresh("vendor"))
        for _ in range(5):
            await asyncio.sleep(0)
        remote.delete("vendor", str(uuid4()))
        with pytest.raises(DomainError, match="取消"):
            await task
        assert provider.cancelled and remote.store.accounts() == ()

    asyncio.run(scenario())


def test_shutdown_cancels_and_disabled_automatic_refresh(remote: Remote) -> None:
    assert isinstance(remote.provider, Provider)
    provider = remote.provider
    remote.save(settings(enabled=False), str(uuid4()))

    async def scenario() -> None:
        await settle(remote)
        assert provider.calls == 0
        provider.gate = asyncio.Event()
        task = asyncio.create_task(remote.refresh("vendor"))
        for _ in range(5):
            await asyncio.sleep(0)
        await remote.close()
        with pytest.raises(DomainError):
            await task
        assert provider.cancelled and provider.closed

    asyncio.run(scenario())


def test_legacy_auto_configuration_requires_editing_and_never_refreshes(remote: Remote) -> None:
    assert isinstance(remote.provider, Provider)
    remote.save(settings(), str(uuid4()))
    saved = remote.store.accounts()[0]
    remote.store.execute(
        lambda work: remote.store.save_account(work, replace(saved, site_type="auto"), clear=True)
    )
    asyncio.run(settle(remote))
    assert remote.provider.calls == 0
    snapshot = remote.get("vendor")
    assert snapshot["status"] == "failed" and snapshot["nextRefreshAt"] is None
    with pytest.raises(DomainError) as error:
        asyncio.run(remote.refresh("vendor"))
    assert error.value.code == "REMOTE_SITE_REQUIRED"


def test_old_delete_receipt_does_not_cancel_a_recreated_account(remote: Remote) -> None:
    assert isinstance(remote.provider, Provider)
    provider = remote.provider
    operation = str(uuid4())
    remote.save(settings(enabled=False), str(uuid4()))
    remote.delete("vendor", operation)
    remote.save(settings(enabled=False), str(uuid4()))

    async def scenario() -> None:
        provider.gate = asyncio.Event()
        task = asyncio.create_task(remote.refresh("vendor"))
        for _ in range(5):
            await asyncio.sleep(0)
        snapshot = remote.delete("vendor", operation)
        assert snapshot["account"] is not None and snapshot["refreshing"]
        provider.gate.set()
        await task
        assert remote.get("vendor")["observation"] is not None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes",
    [
        {"baseUrl": "http://example.test"},
        {"authMode": "access_token", "userId": None},
        {"quotaPerUnit": "0"},
        {"refreshMinutes": 0},
        {"refreshMinutes": 61},
        {"siteType": "unknown"},
        {"siteType": "newapi", "authMode": "api_key"},
        {"siteType": "sub2api", "authMode": "access_token", "userId": "42"},
    ],
)
def test_invalid_settings_do_not_store_credentials(remote: Remote, changes: dict[str, object]) -> None:
    with pytest.raises(DomainError):
        remote.save(settings(**changes), str(uuid4()))
    assert remote.store.accounts() == ()


@pytest.mark.parametrize(
    "changes",
    [{"baseUrl": "https://other.test"}, {"siteType": "sub2api", "authMode": "api_key", "userId": None}],
)
def test_setting_change_cancels_inflight_and_invalidates_previous_result(
    remote: Remote, changes: dict[str, object]
) -> None:
    assert isinstance(remote.provider, Provider)
    provider = remote.provider
    remote.save(settings(enabled=False), str(uuid4()))
    asyncio.run(remote.refresh("vendor"))

    async def scenario() -> None:
        provider.gate = asyncio.Event()
        task = asyncio.create_task(remote.refresh("vendor"))
        for _ in range(5):
            await asyncio.sleep(0)
        remote.save(settings(**changes, credential=None, enabled=False), str(uuid4()))
        with pytest.raises(DomainError):
            await task
        assert remote.get("vendor")["observation"] is None

    asyncio.run(scenario())
