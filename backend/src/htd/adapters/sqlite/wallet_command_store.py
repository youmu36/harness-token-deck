"""Wallet command storage and paged vouchers with exact daily consumption totals."""

import sqlite3
import time
from collections import OrderedDict
from threading import Lock
from typing import cast
from uuid import uuid4

from htd.adapters.sqlite.account_book import AccountBook
from htd.adapters.sqlite.command_store import SqliteCommandStore, connection, read_snapshot
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.remote_repo import RemoteRepository
from htd.adapters.sqlite.wallet_closing import close_days
from htd.adapters.sqlite.wallet_entries import DailySpending, daily_spending, wallet_entries
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.adapters.sqlite.writer import WriteExecutor
from htd.application.ports import UnitOfWork, VendorSnapshot
from htd.domain.common.errors import DomainError
from htd.domain.common.money import parse_usd_amount
from htd.domain.common.site_url import site_url
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.wallet.ledger import attribute_consumption
from htd.domain.wallet.vendors import require_active


def account_amount(parameters: dict[str, object], name: str) -> int:
    exact = parameters.get(name + "Pico")
    decimal = parameters.get(name)
    if (exact is None) == (decimal is None):
        raise DomainError("ACCOUNT_AMOUNT_INVALID", "请提供唯一的精确金额或 USD 金额")
    if exact is not None:
        from htd.adapters.sqlite.pico_codec import decode_pico

        return decode_pico(cast(str, exact))
    return parse_usd_amount(cast(str, decimal))


class SqliteWalletCommandStore(SqliteCommandStore):
    def __init__(self, writer: WriteExecutor, readers: ReadPool) -> None:
        super().__init__(writer, readers)
        self._entries_lock = Lock()
        self._entries_revision: dict[str, tuple[int, int]] = {}
        self._entries_at = 0.0
        self._daily_cache: OrderedDict[tuple[str, int], DailySpending] = OrderedDict()
        self._daily_bytes = 0
        self._entries_cache: OrderedDict[tuple[str, int | None, int, str, int], dict[str, object]] = (
            OrderedDict()
        )

    def release_cache(self) -> None:
        with self._entries_lock:
            self._entries_cache.clear()
            self._entries_revision.clear()
            self._daily_cache.clear()
            self._daily_bytes = 0

    def expire_idle_cache(self) -> None:
        with self._entries_lock:
            if time.monotonic() - self._entries_at >= 60:
                self._entries_cache.clear()
                self._entries_revision.clear()
                self._daily_cache.clear()
                self._daily_bytes = 0

    def wallets(
        self,
        *,
        at: Instant,
        include_archived: bool,
        work: UnitOfWork | None = None,
        vendor_id: str | None = None,
    ) -> tuple[tuple[VendorSnapshot, ...], int]:
        if work is not None:
            return self._wallets(connection(work), at, include_archived, work, vendor_id)
        with read_snapshot(self.readers) as db:
            return self._wallets(db, at, include_archived, vendor_id=vendor_id)

    @staticmethod
    def _wallets(
        db: sqlite3.Connection,
        at: Instant,
        include_archived: bool,
        work: UnitOfWork | None = None,
        vendor_id: str | None = None,
    ) -> tuple[tuple[VendorSnapshot, ...], int]:
        repo = WalletRepository(db)
        from htd.adapters.sqlite.local_wallet import remote_summary, summary
        from htd.adapters.sqlite.wallet_pending import display
        from htd.adapters.sqlite.writer import UnitOfWork as SqlWork

        snapshots: list[VendorSnapshot] = []
        visibility = dict(db.execute("SELECT vendor_id,mode FROM vendor_wallet_visibility"))
        selected = (
            (repo.vendor(vendor_id),) if vendor_id else repo.list_vendors(include_archived=include_archived)
        )
        for vendor in selected:
            if vendor.status == "archived" and not include_archived:
                continue
            staged: dict[str, list[tuple[int, int]]] = {"local": [], "remote": []}
            if work is not None:
                for key, value in cast(SqlWork, work).wallet_flows.items():
                    if key[0] == vendor.id:
                        staged[key[1]].append((value[3], value[4]))
            coverage = db.execute(
                "SELECT value FROM runtime_state WHERE key=?", ("history_coverage_start:" + vendor.id,)
            ).fetchone()
            totals = repo.totals(vendor.id)
            snapshots.append(
                VendorSnapshot(
                    vendor,
                    totals,
                    repo.linked_groups(vendor.id, at=at),
                    parse_instant(coverage[0]) if coverage else None,
                    AccountBook(db).summary(vendor.id, local_totals=(totals.r, totals.c, totals.a)),
                    display(
                        db,
                        vendor.id,
                        "local",
                        summary(db, vendor.id, vendor_record=vendor, totals=totals),
                        staged["local"],
                    ),
                    display(db, vendor.id, "remote", remote_summary(db, vendor.id), staged["remote"]),
                    visibility.get(vendor.id, "both"),
                )
            )
        return tuple(snapshots), db.execute("SELECT value FROM revision WHERE scope='wallet'").fetchone()[0]

    def wallet_entries(
        self,
        vendor_id: str,
        cursor: int | None,
        limit: int,
        kind: str = "all",
        offset_minutes: int = 0,
        *,
        at: Instant | None = None,
    ) -> dict[str, object]:
        from datetime import UTC, datetime

        self.writer.execute(
            lambda work: close_days(work, work.connection, at=at or Instant(datetime.now(UTC)))
        )
        key = vendor_id, cursor, limit, kind, offset_minutes
        with self._entries_lock, read_snapshot(self.readers) as db:
            revision = db.execute("SELECT value FROM revision WHERE scope='wallet'").fetchone()[0]
            now = time.monotonic()
            if now - self._entries_at >= 60:
                self._entries_cache.clear()
                self._entries_revision.clear()
                self._daily_cache.clear()
                self._daily_bytes = 0
            entry_revision = (
                revision,
                db.execute("SELECT value FROM revision WHERE scope='display'").fetchone()[0],
            )
            if self._entries_revision.get(vendor_id) != entry_revision:
                for cached_key in list(self._entries_cache):
                    if cached_key[0] == vendor_id:
                        del self._entries_cache[cached_key]
                for daily_key in list(self._daily_cache):
                    if daily_key[0] == vendor_id:
                        self._daily_bytes -= len(self._daily_cache.pop(daily_key)) * 400
                self._entries_revision[vendor_id] = entry_revision
            self._entries_at = now
            if key not in self._entries_cache:
                daily_key = vendor_id, offset_minutes
                daily = self._daily_cache.get(daily_key)
                if daily is None and kind in ("all", "consumption_daily"):
                    daily = daily_spending(db, vendor_id, offset_minutes)
                    if daily is not None:
                        size = len(daily) * 400
                        while self._daily_cache and (
                            len(self._daily_cache) >= 8 or self._daily_bytes + size > 4 * 1024**2
                        ):
                            _, removed = self._daily_cache.popitem(last=False)
                            self._daily_bytes -= len(removed) * 400
                        self._daily_cache[daily_key] = daily
                        self._daily_bytes += size
                if daily_key in self._daily_cache:
                    self._daily_cache.move_to_end(daily_key)
                self._entries_cache[key] = wallet_entries(
                    db, vendor_id, cursor, limit, kind, offset_minutes, daily=daily
                )
                if len(self._entries_cache) > 8:
                    self._entries_cache.popitem(last=False)
            self._entries_cache.move_to_end(key)
            return {**self._entries_cache[key], "revision": revision}

    def preview_interval(self, vendor_id: str, interval_id: str) -> dict[str, object]:
        with read_snapshot(self.readers) as db:
            return AccountBook(db).preview_interval(vendor_id, interval_id)

    def account_entries(
        self,
        vendor_id: str,
        cursor: str | None,
        limit: int,
        scope: str = "legacy",
        *,
        at: Instant | None = None,
        kind: str = "all",
        start_day: str | None = None,
        end_day: str | None = None,
    ) -> dict[str, object]:
        with read_snapshot(self.readers) as db:
            WalletRepository(db).vendor(vendor_id)
            if scope in ("local", "remote"):
                from datetime import UTC, datetime

                from htd.adapters.sqlite.wallet_flows import entries

                return entries(
                    db,
                    vendor_id,
                    scope,
                    cursor,
                    limit,
                    at or Instant(datetime.now(UTC)),
                    kind,
                    start_day,
                    end_day,
                )
            return AccountBook(db).entries(vendor_id, cursor, limit)

    def daily_book(self, vendor_id: str, scope: str, month: str, at: Instant) -> dict[str, object]:
        from htd.adapters.sqlite.wallet_book import read

        self.writer.execute(lambda work: close_days(work, work.connection, at=at))
        with read_snapshot(self.readers) as db:
            WalletRepository(db).vendor(vendor_id)
            return read(db, vendor_id, scope, month, at)

    def wallet_command(
        self, work: UnitOfWork, command: str, parameters: dict[str, object], operation_id: str, *, at: Instant
    ) -> tuple[str, str | None, tuple[str, ...]]:
        repo = WalletRepository(connection(work))
        vendor_id = cast(str, parameters.get("vendorId"))
        action: str | None = None
        affected: set[str] = set()
        match command:
            case "wallet.dismissNotice":
                from htd.adapters.sqlite.wallet_flows import dismiss_notice

                repo.vendor(vendor_id)
                dismiss_notice(
                    connection(work), work, vendor_id, str(parameters["entryId"]), str(parameters["noticeId"])
                )
            case "wallet.setVisibility":
                repo.vendor(vendor_id)
                db = connection(work)
                old = db.execute(
                    "SELECT mode FROM vendor_wallet_visibility WHERE vendor_id=?", (vendor_id,)
                ).fetchone()
                if parameters["mode"] != (old[0] if old else "both"):
                    db.execute(
                        "INSERT INTO vendor_wallet_visibility VALUES(?,?) ON CONFLICT(vendor_id) "
                        "DO UPDATE SET mode=excluded.mode",
                        (vendor_id, parameters["mode"]),
                    )
                    work.touch("wallet")
            case "wallet.correctEntry":
                from htd.adapters.sqlite.wallet_flows import correct

                correct(connection(work), work, vendor_id, parameters, operation_id, at)
            case "wallet.setLocalTotals":
                from htd.adapters.sqlite.local_wallet import set_totals, summary

                if (
                    parameters.get("R") is not None
                    and parameters["R"] != summary(connection(work), vendor_id)["R"]
                ):
                    raise DomainError(
                        "LOCAL_TOP_UP_REQUIRED", "累计充值不能直接修改，请填写本次充值或校准原充值流水"
                    )
                set_totals(connection(work), work, vendor_id, parameters, operation_id, at)
            case "wallet.createScannedVendors":
                for row in cast(list[dict[str, str]], parameters["providers"]):
                    if any(v.url == site_url(row["url"]) for v in repo.list_vendors(include_archived=True)):
                        raise DomainError("PROVIDER_ALREADY_SAVED", "此网址已有供应商，请重新扫描")
                    vendor_id = str(uuid4())
                    repo.create_vendor(
                        work,
                        vendor_id=vendor_id,
                        name=row["name"],
                        url=row["url"],
                        at=at,
                        operation_id=operation_id,
                        record_operation=False,
                    )
                    affected.add(vendor_id)
            case "wallet.createVendor":
                vendor_id = str(uuid4())
                repo.create_vendor(
                    work,
                    vendor_id=vendor_id,
                    name=cast(str, parameters["name"]),
                    at=at,
                    operation_id=operation_id,
                    initial_balance=cast(str | None, parameters.get("initialBalance")),
                    past_top_up=cast(str | None, parameters.get("pastTopUp")),
                    past_consumption=cast(str | None, parameters.get("pastConsumption")),
                    url=cast(str | None, parameters.get("url")),
                    record_operation=False,
                )
            case "wallet.renameVendor":
                repo.rename_vendor(work, vendor_id, cast(str, parameters["name"]))
                if "pastTopUp" in parameters:
                    repo.set_past_top_up(work, vendor_id, cast(str, parameters["pastTopUp"]))
                if "pastConsumption" in parameters:
                    repo.set_past_consumption(work, vendor_id, cast(str, parameters["pastConsumption"]))
                if "url" in parameters:
                    new_url = cast(str | None, parameters["url"])
                    account = next(
                        (
                            a
                            for a in RemoteRepository(connection(work)).accounts()
                            if a.vendor_id == vendor_id
                        ),
                        None,
                    )
                    if account and site_url(new_url) != site_url(account.base_url):
                        raise DomainError("REMOTE_SITE_MANAGED", "已配置远程账户，请在远程账户中修改站点地址")
                    repo.set_site(work, vendor_id, new_url)
            case "wallet.topUp":
                if parameters.get("walletScope") == "remote":
                    require_active(repo.vendor(vendor_id))
                    book = AccountBook(connection(work))
                    segment = book.active(vendor_id)
                    if segment is None or segment["kind"] != "remote":
                        raise DomainError("REMOTE_ACCOUNT_REQUIRED", "请先配置远程账户")
                    amount = parse_usd_amount(cast(str, parameters["amount"]))
                    if amount <= 0:
                        raise DomainError("ACCOUNT_AMOUNT_INVALID", "充值金额须大于零")
                    book.post(
                        work,
                        vendor=vendor_id,
                        segment=segment["id"],
                        interval=None,
                        at=format_instant(at),
                        kind="manual_top_up",
                        r=amount,
                        note="远程钱包手工充值(待匹配账户区间)",
                        operation=operation_id,
                    )
                    work.touch("wallet")
                    return vendor_id, None, (vendor_id,)
                repo.top_up(
                    work,
                    vendor_id,
                    cast(str, parameters["amount"]),
                    at=at,
                    operation_id=operation_id,
                    record_operation=False,
                )
            case "wallet.reconcileInterval":
                require_active(repo.vendor(vendor_id))
                AccountBook(connection(work)).revise_interval(
                    work,
                    vendor_id,
                    cast(str, parameters["intervalId"]),
                    cast(str, parameters["fingerprint"]),
                    cast(int, parameters["expectedAccountRevision"]),
                    operation_id,
                    at,
                )
            case "wallet.reclassify":
                require_active(repo.vendor(vendor_id))
                if parameters.get("scope") == "local":
                    from htd.adapters.sqlite.local_wallet import reclassify

                    reclassify(connection(work), work, vendor_id, parameters, operation_id, at)
                    return vendor_id, None, (vendor_id,)
                AccountBook(connection(work)).reclassify(
                    work,
                    vendor_id,
                    cast(str, parameters["entryId"]),
                    account_amount(parameters, "amount"),
                    cast(str, parameters["targetKind"]),
                    cast(str, parameters.get("note", "")),
                    cast(int, parameters["expectedAccountRevision"]),
                    operation_id,
                    at,
                )
            case "wallet.correctConsumption":
                require_active(repo.vendor(vendor_id))
                AccountBook(connection(work)).correct_consumption(
                    work,
                    vendor_id,
                    account_amount(parameters, "target"),
                    cast(int, parameters["expectedAccountRevision"]),
                    cast(str, parameters.get("note", "")),
                    operation_id,
                    at,
                    remote_only=parameters.get("scope") == "remote",
                )
            case "wallet.calibrate":
                exact: int | None = None
                if parameters.get("remoteFetchedAt") is not None:
                    remote = RemoteRepository(connection(work))
                    account = next((a for a in remote.accounts() if a.vendor_id == vendor_id), None)
                    latest = remote.latest(account.id) if account else None
                    observation = latest.balance() if latest else None
                    if (
                        account is None
                        or observation is None
                        or not observation.can_calibrate
                        or (
                            format_instant(observation.fetched_at) != parameters["remoteFetchedAt"]
                            or (at.value - observation.fetched_at.value).total_seconds()
                            > account.refresh_minutes * 120
                        )
                    ):
                        raise DomainError(
                            "REMOTE_OBSERVATION_CHANGED", "远程观测已变化或不可校准，请刷新后重新确认"
                        )
                    assert observation.amount_pico is not None
                    exact = observation.amount_pico
                    displayed = (abs(exact) // 10**6) * 10**6 * (-1 if exact < 0 else 1)
                    if parse_usd_amount(cast(str, parameters["target"]), allow_negative=True) != displayed:
                        raise DomainError(
                            "REMOTE_OBSERVATION_CHANGED", "远程预填金额已修改，请重新选择按此余额校准"
                        )
                book = AccountBook(connection(work))
                segment = book.active(vendor_id)
                target = (
                    exact
                    if exact is not None
                    else parse_usd_amount(cast(str, parameters["target"]), allow_negative=True)
                )
                if segment and segment["kind"] == "remote":
                    book.calibrate_balance(
                        work,
                        vendor_id,
                        target,
                        operation_id,
                        at,
                        remote_only=parameters.get("walletScope") == "remote",
                    )
                else:
                    if parameters.get("walletScope") == "remote":
                        raise DomainError("REMOTE_ACCOUNT_REQUIRED", "请先配置远程账户")
                    before = repo.totals(vendor_id).b
                    repo.calibrate(
                        work,
                        vendor_id,
                        cast(str, parameters["target"]),
                        at=at,
                        expected_revision=cast(int, parameters["expectedWalletRevision"]),
                        operation_id=operation_id,
                        record_operation=False,
                        target_pico=exact,
                    )
                    book.calibrate_balance(
                        work, vendor_id, target, operation_id, at, local_delta=target - before
                    )
            case "wallet.linkGroups":
                require_active(repo.vendor(vendor_id))
                desired = set(cast(list[str], parameters["groupIds"]))
                owned = set(repo.linked_groups(vendor_id, at=at))
                links = repo.links()
                for group_id in sorted(owned - desired):
                    repo.link_group(work, group_id, None, at=at)
                for group_id in sorted(desired - owned):
                    previous = attribute_consumption(links, group_id, at)
                    if previous is not None:
                        affected.add(previous)
                        repo.link_group(work, group_id, None, at=at)
                    repo.link_group(work, group_id, vendor_id, at=at)
            case "wallet.deleteVendor":
                if repo.vendor(vendor_id).status == "archived":
                    action = "archive"
                else:
                    action = repo.delete_vendor(work, vendor_id, at=at)
            case "wallet.restoreVendor":
                repo.restore_vendor(work, vendor_id)
            case _:
                raise ValueError("Unknown wallet use case")
        return vendor_id, action, tuple(sorted(affected | {vendor_id}))
