"""Account commands and per-account refresh policy on one background event loop."""

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from threading import RLock
from typing import cast
from uuid import uuid4

from htu.application.command import execute
from htu.application.ports import UnitOfWork, WalletChanged
from htu.application.ports.remote import RemoteStore
from htu.application.remote.ports import (
    CredentialStore,
    credential_reference,
    normalize_credential,
    validate_credential,
)
from htu.domain.common.errors import DomainError
from htu.domain.common.site_url import site_url
from htu.domain.common.time import Clock, Instant, format_instant
from htu.domain.remote.model import BalanceKind, BalanceObservation, RemoteAccount, RemoteFailure
from htu.domain.remote.ports import BalanceProvider


def observation_dict(value: BalanceObservation | None, *, allowed: bool = True) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "accountId": value.account_id,
        "fetchedAt": format_instant(value.fetched_at),
        "kind": value.kind.value,
        "source": value.source,
        "currency": value.currency,
        "amountPico": str(value.amount_pico) if value.amount_pico is not None else None,
        "rawQuota": str(value.raw_quota) if value.raw_quota is not None else None,
        "quotaPerUnit": str(value.quota_per_unit) if value.quota_per_unit is not None else None,
        "stale": value.stale,
        "currencyUncertain": value.currency_uncertain,
        "displayInCurrency": value.display_in_currency,
        "canCalibrate": allowed and value.can_calibrate,
        "cumulativeUsedPico": str(value.cumulative_used_pico)
        if value.cumulative_used_pico is not None
        else None,
        "usageScope": value.usage_scope,
        "originalCurrency": value.original_currency,
        "conversionRule": value.conversion_rule,
    }


def failure_dict(error: RemoteFailure | None) -> dict[str, object] | None:
    return (
        None
        if error is None
        else {
            "code": error.code,
            "message": error.message,
            "statusCode": error.status_code,
            "retryAfterSeconds": error.retry_after_seconds,
        }
    )


@dataclass(slots=True)
class RefreshState:
    account: RemoteAccount
    due: float
    auth_failed: bool = False
    limited_until: float = 0
    failures: int = 0
    stale: bool = False
    task: asyncio.Task[None] | None = None
    stale_due: float | None = None
    retry_attempt: int = 0


class Remote:
    def __init__(
        self, store: RemoteStore, credentials: CredentialStore, provider: BalanceProvider, clock: Clock
    ) -> None:
        self.store, self.credentials, self.provider, self.clock = store, credentials, provider, clock
        self.states: dict[str, RefreshState] = {}
        self._commands = RLock()
        self.closed = False
        self.suspended = False
        self.wake: Callable[[], None] = lambda: None
        self.store.begin_run(self.clock.now_utc())
        self._recover_credentials()

    def _recover_credentials(self) -> None:
        # Journal is staged before vault writes. Run only on startup or after a known
        # commit; a WRITE_TIMEOUT leaves the new reference intact for receipt replay.
        for reference in self.store.orphan_credentials():
            try:
                self.credentials.delete(reference)
                self.store.forget_credential(reference)
            except Exception:
                continue  # Retry on next startup; active credentials are never deleted.

    def _account(self, vendor_id: str) -> RemoteAccount | None:
        return next((a for a in self.store.accounts() if a.vendor_id == vendor_id), None)

    def _state(self, account: RemoteAccount) -> RefreshState:
        if account.id not in self.states:
            history = self.store.observations(account.id)
            latest = history[0] if history else None
            due = self.clock.monotonic()
            state = RefreshState(account, due, auth_failed=self.store.auth_failed(account.id))
            if latest is not None:
                age = max(0.0, (self.clock.now_utc().value - latest.fetched_at.value).total_seconds())
                state.auth_failed |= latest.error is not None and latest.error.code == "auth_failed"
                delay = account.refresh_minutes * 60
                if latest.error is not None and latest.error.code == "rate_limited":
                    state.failures = 1
                    delay = (
                        latest.error.retry_after_seconds
                        if latest.error.retry_after_seconds is not None
                        else 60
                    )
                    state.limited_until = due + max(0.0, delay - age)
                state.due = due + max(0.0, delay - age)
                if latest.error is not None and latest.error.code not in ("auth_failed", "rate_limited"):
                    recent_failures = 0
                    for observation in history:
                        if observation.error is None:
                            break
                        recent_failures += 1
                    if account.refresh_minutes >= 5 and recent_failures <= 2:
                        state.retry_attempt = recent_failures
                        state.due = due + max(0.0, 10 - age)
            self.states[account.id] = state
        return self.states[account.id]

    def get(self, vendor_id: str) -> dict[str, object]:
        account = self._account(vendor_id)
        base: dict[str, object] = {
            "account": None,
            "observation": None,
            "status": "unconfigured",
            "error": None,
            "nextRefreshAt": None,
            "refreshing": False,
            "revision": self.store.revisions()["wallet"],
        }
        if account is None:
            return base
        state = self._state(account)
        history = self.store.observations(account.id)
        latest = history[0] if history else None
        success = next((o for o in history if o.error is None), None)
        if success is not None:
            success = replace(
                success,
                stale=(self.clock.now_utc().value - success.fetched_at.value).total_seconds()
                > account.refresh_minutes * 120,
            )
        error = latest.error if latest else None
        if not account.configured:
            error = RemoteFailure("configuration_required", "请编辑远程账户，选择接口类型并填写对应凭据")
        status = (
            "auth_failed"
            if state.auth_failed
            else "rate_limited"
            if error and error.code == "rate_limited"
            else "offline"
            if error and error.code in ("offline", "timeout", "tls_failed")
            else "failed"
            if error
            else "stale"
            if success and success.stale
            else "ok"
            if success
            else "pending"
        )
        base.update(
            {
                "account": {
                    "id": account.id,
                    "vendorId": account.vendor_id,
                    "baseUrl": account.base_url,
                    "siteType": account.site_type,
                    "authMode": account.auth_mode,
                    "userId": account.user_id,
                    "quotaPerUnit": str(account.quota_per_unit)
                    if account.quota_per_unit is not None
                    else None,
                    "refreshMinutes": account.refresh_minutes,
                    "enabled": account.enabled,
                    "maskedCredential": self.credentials.masked_tail(account.credential_ref),
                },
                "observation": observation_dict(success, allowed=error is None and not state.auth_failed),
                "status": status,
                "error": failure_dict(error),
                "refreshing": state.task is not None,
                "nextRefreshAt": format_instant(
                    Instant(
                        self.clock.now_utc().value
                        + timedelta(seconds=max(0.0, state.due - self.clock.monotonic()))
                    )
                )
                if account.configured
                and (
                    ((account.enabled or state.retry_attempt > 0) and not state.auth_failed)
                    or state.limited_until > self.clock.monotonic()
                )
                else None,
            }
        )
        return base

    def _draft(self, parameters: dict[str, object], account_id: str) -> RemoteAccount:
        if parameters.get("siteType") not in ("newapi", "sub2api", "deepseek"):
            raise DomainError(
                "REMOTE_SITE_REQUIRED", "请选择 New API、sub2api 或 DeepSeek 官方接口", "siteType"
            )
        expected = "access_token" if parameters["siteType"] == "newapi" else "api_key"
        if parameters.get("authMode", "api_key") != expected:
            raise DomainError(
                "REMOTE_ACCOUNT_INVALID", "New API 须使用访问令牌和用户 ID，其余接口须使用 API 密钥"
            )
        try:
            quota = parameters.get("quotaPerUnit")
            return RemoteAccount(
                account_id,
                cast(str, parameters["vendorId"]),
                site_url(cast(str, parameters["baseUrl"])) or "",
                credential_reference(account_id),
                cast(str, parameters.get("authMode", "api_key")),
                cast(str | None, parameters.get("userId")),
                int(cast(str, quota)) if quota else None,
                cast(int, parameters.get("refreshMinutes", 5)),
                cast(bool, parameters.get("enabled", True)),
                site_type=cast(str, parameters["siteType"]),
            )
        except ValueError:
            raise DomainError(
                "REMOTE_ACCOUNT_INVALID", "请检查站点地址、认证方式、用户 ID 与刷新设置"
            ) from None

    def _credential(self, parameters: dict[str, object], account: RemoteAccount) -> str:
        credential = normalize_credential(
            cast(str | None, parameters.get("credential"))
        ) or self.credentials.get(account.credential_ref)
        if not credential:
            raise DomainError("REMOTE_CREDENTIAL_REQUIRED", "请填写 API 令牌或访问令牌", "credential")
        validate_credential(credential)
        return credential

    def save(self, parameters: dict[str, object], operation_id: str) -> dict[str, object]:
        with self._commands:
            return self._save(parameters, operation_id)

    def _save(self, parameters: dict[str, object], operation_id: str) -> dict[str, object]:
        old = self._account(cast(str, parameters["vendorId"]))
        account = self._draft(parameters, old.id if old else str(uuid4()))
        credential = normalize_credential(cast(str | None, parameters.get("credential")))
        if credential:
            validate_credential(credential)
        safe = {k: v for k, v in parameters.items() if k != "credential"}
        safe["credentialDigest"] = (
            hashlib.sha256((operation_id + credential).encode()).hexdigest() if credential else None
        )
        if credential:
            account = replace(account, credential_ref=credential_reference(account.id) + "/v_" + uuid4().hex)
            self.store.stage_credential(account.credential_ref, account.id)
            self.credentials.put(account.credential_ref, credential)
        elif old is not None:
            account = replace(account, credential_ref=old.credential_ref)
        if self.credentials.get(account.credential_ref) is None:
            raise DomainError("REMOTE_CREDENTIAL_REQUIRED", "首次保存须填写令牌", "credential")
        reset = (
            old is None
            or bool(credential)
            or (old.base_url, old.site_type, old.auth_mode, old.user_id, old.quota_per_unit)
            != (
                account.base_url,
                account.site_type,
                account.auth_mode,
                account.user_id,
                account.quota_per_unit,
            )
        )
        performed = False

        def action(work: UnitOfWork, at: Instant) -> dict[str, object]:
            nonlocal performed
            self.store.save_account(work, account, clear=reset, at=at)
            performed = True
            return {"saved": True}

        execute(self.store, self.clock, "remote.save", safe, operation_id, action)
        self._recover_credentials()
        if not performed:
            return self.get(account.vendor_id)
        # A receipt replay must use the persisted ID, including the first save.
        persisted = self._account(account.vendor_id)
        assert persisted is not None
        state = self.states.get(persisted.id)
        if state is not None and reset:
            if state.task is not None:
                state.task.cancel()
            del self.states[persisted.id]
        elif state is not None and state.account != persisted:
            state.account = persisted
            state.retry_attempt = 0
            state.due = max(state.limited_until, self.clock.monotonic() + persisted.refresh_minutes * 60)
        return self.get(account.vendor_id)

    async def test(self, parameters: dict[str, object]) -> dict[str, object]:
        saved = self._account(cast(str, parameters["vendorId"]))
        account = self._draft(parameters, saved.id if saved else "test-" + uuid4().hex)
        if saved is not None:
            account = replace(account, credential_ref=saved.credential_ref)
        result = await self.provider.fetch(account, self._credential(parameters, account))
        return {"observation": observation_dict(result), "error": failure_dict(result.error)}

    async def _fetch(self, state: RefreshState) -> None:
        account = state.account
        try:
            credential = self.credentials.get(account.credential_ref)
            if credential is None:
                result = BalanceObservation(
                    account.id,
                    self.clock.now_utc(),
                    BalanceKind.TOKEN_REMAINING,
                    "unknown",
                    error=RemoteFailure("auth_failed", "已保存的凭据不存在，请重新填写令牌"),
                )
            else:
                result = await self.provider.fetch(account, credential)
            if (
                self.closed
                or self.suspended
                or self._account(account.vendor_id) != account
                or self.states.get(account.id) is not state
            ):
                return
            now = self.clock.monotonic()
            if result.error and result.error.code == "auth_failed":
                state.auth_failed = True
                state.retry_attempt = 0
            elif result.error and result.error.code == "rate_limited":
                state.retry_attempt = 0
                state.failures += 1
                delay = result.error.retry_after_seconds
                if delay is None:
                    delay = min(30, 2 ** min(state.failures - 1, 5)) * 60
                state.limited_until = now + delay
                state.due = state.limited_until
            else:
                state.failures = 0
                state.limited_until = 0
                if result.error is not None and account.refresh_minutes >= 5 and state.retry_attempt < 2:
                    state.retry_attempt += 1
                    state.due = now + 10
                else:
                    state.retry_attempt = 0
                    state.due = now + account.refresh_minutes * 60
            # No await between the deletion guard and the atomic observation transaction.
            state.task = None
            self.store.execute(lambda work: self.store.observe(work, result))
        except asyncio.CancelledError:
            raise
        finally:
            state.task = None
            self.wake()

    async def refresh(self, vendor_id: str, operation_id: str | None = None) -> dict[str, object]:
        if self.closed or self.suspended:
            raise DomainError("APP_SHUTTING_DOWN", "远程刷新正在停止，请稍后重试")
        parameters: dict[str, object] = {"vendorId": vendor_id}
        if operation_id:
            replay = self.store.execute(
                lambda work: self.store.replay(work, operation_id, "remote.refresh", parameters)
            )
            if replay is not None:
                return replay
        account = self._account(vendor_id)
        if account is None:
            raise DomainError("REMOTE_ACCOUNT_NOT_FOUND", "请先配置远程账户")
        if not account.configured:
            raise DomainError("REMOTE_SITE_REQUIRED", "请编辑远程账户，选择接口类型并填写对应凭据")
        state = self._state(account)
        if self.clock.monotonic() >= state.limited_until:
            if state.task is None:
                state.retry_attempt = 0
                state.task = asyncio.create_task(self._fetch(state))
            try:
                await asyncio.shield(state.task)
            except asyncio.CancelledError:
                raise DomainError("REMOTE_REFRESH_CANCELLED", "远程账户已变更或刷新已取消") from None
        result = self.get(vendor_id)
        if operation_id:
            return execute(
                self.store, self.clock, "remote.refresh", parameters, operation_id, lambda work, at: result
            )
        return result

    def delete(self, vendor_id: str, operation_id: str) -> dict[str, object]:
        with self._commands:
            return self._delete(vendor_id, operation_id)

    def _delete(self, vendor_id: str, operation_id: str) -> dict[str, object]:
        account = self._account(vendor_id)
        parameters: dict[str, object] = {"vendorId": vendor_id}
        performed = False

        def action(work: UnitOfWork, at: Instant) -> dict[str, object]:
            nonlocal performed
            if account is not None:
                self.store.delete_account(work, account.id)
                work.emit(WalletChanged((vendor_id,), work.revision("wallet")))
            performed = True
            return {"deleted": True}

        execute(self.store, self.clock, "remote.delete", parameters, operation_id, action)
        self._recover_credentials()
        if performed and account is not None:
            state = self.states.pop(account.id, None)
            if state and state.task:
                state.task.cancel()
        return self.get(vendor_id)

    async def tick(self) -> None:
        if self.closed or self.suspended:
            return
        self.store.flush_pending(self.clock.now_utc())
        accounts = self.store.accounts(active_only=True)
        active = {a.id for a in accounts}
        for account_id in tuple(self.states):
            if account_id not in active:
                state = self.states.pop(account_id)
                if state.task:
                    state.task.cancel()
        for account in accounts:
            state = self._state(account)
            success = next((o for o in self.store.observations(account.id) if o.error is None), None)
            stale = (
                success is not None
                and (self.clock.now_utc().value - success.fetched_at.value).total_seconds()
                > account.refresh_minutes * 120
            )
            if stale != state.stale:
                state.stale = stale
                self.store.notify(account.vendor_id)
            state.stale_due = (
                self.clock.monotonic()
                + max(
                    0.0,
                    account.refresh_minutes * 120
                    - (self.clock.now_utc().value - success.fetched_at.value).total_seconds(),
                )
                + 0.01
                if success is not None and not stale
                else None
            )
            if (
                account.configured
                and (account.enabled or state.retry_attempt > 0)
                and not state.auth_failed
                and state.task is None
                and self.clock.monotonic() >= state.due
            ):
                state.task = asyncio.create_task(self._fetch(state))
                state.task.add_done_callback(self._consume)

    def next_tick_delay(self) -> float:
        if self.closed or self.suspended:
            return 30
        now = self.clock.monotonic()
        deadlines = [now + 30]
        for state in self.states.values():
            if state.stale_due is not None:
                deadlines.append(state.stale_due)
            if (
                (state.account.enabled or state.retry_attempt > 0)
                and state.account.configured
                and not state.auth_failed
                and state.task is None
            ):
                deadlines.append(state.due)
        return max(0.05, min(deadlines) - now)

    @staticmethod
    def _consume(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()  # Failures stay isolated from the collection coordinator.

    async def suspend(self) -> None:
        self.suspended = True
        tasks = [s.task for s in self.states.values() if s.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.states.clear()

    async def close(self) -> None:
        self.closed = True
        await self.suspend()
        await self.provider.aclose()
