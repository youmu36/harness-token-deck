"""Persist credential references and the newest fifty remote observations per account."""

import re
import sqlite3
from dataclasses import dataclass

from htu.adapters.sqlite.pico_codec import decode_pico, encode_pico
from htu.adapters.sqlite.transaction import require_transaction
from htu.application.ports import UnitOfWork
from htu.domain.common.redaction import redact_credentials
from htu.domain.common.time import Instant, format_instant, parse_instant
from htu.domain.remote.model import BalanceKind, BalanceObservation, RemoteFailure
from htu.domain.remote.model import RemoteAccount as RemoteAccount

_KINDS = {"token_remaining", "account_remaining", "subscription_remaining", "unlimited", "quota_units"}


@dataclass(frozen=True, slots=True)
class RemoteObservation:
    account_id: str
    fetched_at: Instant
    ok: bool
    kind: str
    amount_pico: int | None = None
    raw_quota: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    currency: str | None = None
    quota_per_unit: int | None = None
    source: str = "unknown"
    currency_uncertain: bool = True
    display_in_currency: bool | None = None
    status_code: int | None = None
    retry_after_seconds: int | None = None
    cumulative_used_pico: int | None = None
    usage_scope: str | None = None
    original_currency: str | None = None
    conversion_rule: str | None = None

    def balance(self) -> BalanceObservation:
        return BalanceObservation(
            self.account_id,
            self.fetched_at,
            BalanceKind(self.kind),
            self.source,
            self.currency,
            self.amount_pico,
            int(self.raw_quota) if self.raw_quota is not None else None,
            self.quota_per_unit,
            currency_uncertain=self.currency_uncertain,
            display_in_currency=self.display_in_currency,
            error=RemoteFailure(
                self.error_code or "remote_failed",
                self.error_message or "远程查询失败",
                self.status_code,
                self.retry_after_seconds,
            )
            if not self.ok
            else None,
            cumulative_used_pico=self.cumulative_used_pico,
            usage_scope=self.usage_scope,
            original_currency=self.original_currency,
            conversion_rule=self.conversion_rule,
        )

    @classmethod
    def from_balance(cls, value: BalanceObservation) -> "RemoteObservation":
        error = value.error
        return cls(
            value.account_id,
            value.fetched_at,
            error is None,
            value.kind.value,
            value.amount_pico,
            str(value.raw_quota) if value.raw_quota is not None else None,
            error.code if error else None,
            error.message if error else None,
            value.currency,
            value.quota_per_unit,
            value.source,
            value.currency_uncertain,
            value.display_in_currency,
            error.status_code if error else None,
            error.retry_after_seconds if error else None,
            value.cumulative_used_pico,
            value.usage_scope,
            value.original_currency,
            value.conversion_rule,
        )

    def __post_init__(self) -> None:
        if (
            type(self.account_id) is not str
            or not self.account_id
            or type(self.fetched_at) is not Instant
            or type(self.ok) is not bool
            or self.kind not in _KINDS
        ):
            raise ValueError("Invalid remote observation")
        if self.amount_pico is not None:
            encode_pico(self.amount_pico)
        if (not self.ok or self.kind in ("unlimited", "quota_units")) and self.amount_pico is not None:
            raise ValueError("This observation cannot carry a USD amount")
        if self.raw_quota is not None:
            decode_pico(self.raw_quota)
        if self.error_code is not None and re.fullmatch(r"[A-Za-z0-9_]{1,80}", self.error_code) is None:
            raise ValueError("Invalid stable remote error code")
        if self.error_message is not None:
            object.__setattr__(self, "error_message", redact_credentials(self.error_message)[:200])


class RemoteRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def accounts(self) -> tuple[RemoteAccount, ...]:
        return tuple(
            RemoteAccount(
                row["id"],
                row["vendor_id"],
                row["base_url"],
                row["credential_ref"],
                row["auth_mode"],
                row["user_id"],
                decode_pico(row["quota_per_unit"]) if row["quota_per_unit"] is not None else None,
                row["refresh_minutes"],
                bool(row["enabled"]),
                row["adapter"],
                row["site_type"],
            )
            for row in self.connection.execute("SELECT * FROM remote_account ORDER BY id")
        )

    def save(self, work: UnitOfWork, account: RemoteAccount) -> None:
        require_transaction(work, self.connection)
        if type(account) is not RemoteAccount:
            raise ValueError("Expected RemoteAccount")
        vendor = self.connection.execute(
            "SELECT status FROM vendor WHERE id=?", (account.vendor_id,)
        ).fetchone()
        if vendor is None or vendor[0] != "active":
            raise ValueError("Remote settings require an active vendor")
        prior = self.connection.execute(
            "SELECT vendor_id FROM remote_account WHERE id=?", (account.id,)
        ).fetchone()
        if prior is not None and prior[0] != account.vendor_id:
            raise ValueError("Cannot move a remote account between vendors")
        if account in self.accounts():
            return
        self.connection.execute(
            "INSERT INTO remote_account VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "adapter=excluded.adapter,base_url=excluded.base_url,auth_mode=excluded.auth_mode,"
            "user_id=excluded.user_id,credential_ref=excluded.credential_ref,"
            "quota_per_unit=excluded.quota_per_unit,refresh_minutes=excluded.refresh_minutes,"
            "enabled=excluded.enabled,site_type=excluded.site_type",
            (
                account.id,
                account.vendor_id,
                account.adapter,
                account.base_url,
                account.auth_mode,
                account.user_id,
                account.credential_ref,
                encode_pico(account.quota_per_unit) if account.quota_per_unit is not None else None,
                account.refresh_minutes,
                int(account.enabled),
                account.site_type,
            ),
        )
        work.touch("wallet")

    def delete(self, work: UnitOfWork, account_id: str) -> None:
        require_transaction(work, self.connection)
        if self.connection.execute("DELETE FROM remote_account WHERE id=?", (account_id,)).rowcount:
            work.touch("wallet")

    def observe(self, work: UnitOfWork, observation: RemoteObservation) -> None:
        require_transaction(work, self.connection)
        if type(observation) is not RemoteObservation:
            raise ValueError("Expected RemoteObservation")
        latest = self.latest(observation.account_id)
        values = (
            observation.account_id,
            format_instant(observation.fetched_at),
            int(observation.ok),
            observation.kind,
            encode_pico(observation.amount_pico) if observation.amount_pico is not None else None,
            observation.raw_quota,
            observation.error_code,
            observation.error_message,
            observation.currency,
            encode_pico(observation.quota_per_unit) if observation.quota_per_unit is not None else None,
            observation.source,
            int(observation.currency_uncertain),
            None if observation.display_in_currency is None else int(observation.display_in_currency),
            observation.status_code,
            observation.retry_after_seconds,
            str(observation.cumulative_used_pico) if observation.cumulative_used_pico is not None else None,
            observation.usage_scope,
            observation.original_currency,
            observation.conversion_rule,
        )
        prior = self.connection.execute(
            "SELECT * FROM remote_observation WHERE account_id=? AND fetched_at=?", values[:2]
        ).fetchone()
        if prior is not None:
            if tuple(prior) != values:
                raise ValueError("Observation timestamp already contains different data")
            return
        self.connection.execute(
            "INSERT INTO remote_observation VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
        )
        self.connection.execute(
            "DELETE FROM remote_observation WHERE account_id=? AND fetched_at NOT IN "
            "(SELECT fetched_at FROM remote_observation WHERE account_id=? "
            "ORDER BY fetched_at DESC LIMIT 50)",
            (observation.account_id, observation.account_id),
        )
        if latest is None or any(
            getattr(latest, field) != getattr(observation, field)
            for field in (
                "ok",
                "kind",
                "amount_pico",
                "raw_quota",
                "error_code",
                "error_message",
                "currency",
                "currency_uncertain",
                "source",
                "cumulative_used_pico",
                "usage_scope",
                "original_currency",
                "conversion_rule",
            )
        ):
            work.touch("wallet")

    def observations(
        self, account_id: str, *, successful_only: bool = False
    ) -> tuple[RemoteObservation, ...]:
        return tuple(
            RemoteObservation(
                row["account_id"],
                parse_instant(row["fetched_at"]),
                bool(row["ok"]),
                row["kind"],
                decode_pico(row["amount_pico"]) if row["amount_pico"] is not None else None,
                row["raw_quota"],
                row["error_code"],
                row["error_message"],
                row["currency"],
                decode_pico(row["quota_per_unit"]) if row["quota_per_unit"] is not None else None,
                row["source"],
                bool(row["currency_uncertain"]),
                None if row["display_in_currency"] is None else bool(row["display_in_currency"]),
                row["status_code"],
                row["retry_after_seconds"],
                int(row["cumulative_used"]) if row["cumulative_used"] is not None else None,
                row["usage_scope"],
                row["original_currency"],
                row["conversion_rule"],
            )
            for row in self.connection.execute(
                "SELECT * FROM remote_observation WHERE account_id=? AND (?=0 OR ok=1) "
                "ORDER BY fetched_at DESC",
                (account_id, int(successful_only)),
            )
        )

    def latest(self, account_id: str, *, successful_only: bool = False) -> RemoteObservation | None:
        rows = self.observations(account_id, successful_only=successful_only)
        return rows[0] if rows else None
