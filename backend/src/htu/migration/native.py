"""Restore native ledger backups without passing them through the legacy converter."""

from contextlib import closing
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from htu.adapters.sqlite.connection import open_connection
from htu.adapters.sqlite.migrate import APP_ID, migrate
from htu.adapters.sqlite.usage_archive import archived_rows
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.migration.legacy.reader import SQLiteConnection, backup, open_legacy
from htu.migration.snapshot import sha256_file, snapshot_time

if TYPE_CHECKING:
    from htu.migration.verify import Verification


@dataclass(frozen=True)
class NativeConversion:
    source: Path


def is_native(path: Path) -> bool:
    with closing(open_legacy(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
            return False
        return dict(db.execute("SELECT key,value FROM meta")).get("app_id") == APP_ID


def restore(path: Path, staging: Path) -> NativeConversion:
    backup(path, staging)
    with closing(open_legacy(path)) as db:
        metadata = dict(db.execute("SELECT key,value FROM meta"))
    at = snapshot_time(path, metadata["created_at"])
    migrate(staging, now=at)
    with closing(open_connection(staging)) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("PRAGMA journal_mode=DELETE")
    return NativeConversion(path)


def quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def total(db: SQLiteConnection) -> int:
    from htu.adapters.sqlite.counter_codec import decompress

    return (
        sum(int(decompress(row[0])["pico"]) for row in db.execute("SELECT payload FROM usage_daily"))
        + sum(int(row[0]) for row in db.execute("SELECT pico FROM ledger"))
        + sum(int(row[0]) for row in db.execute("SELECT pico FROM usage_rollup"))
        + sum(
            int(row["pico"])
            for row in archived_rows(db, "0001-01-01T00:00:00.000Z", "9999-01-01T00:00:00.000Z")
        )
    )


def verify_native(source: Path, target: Path) -> "Verification":
    from htu.migration.verify import Verification, WalletCheck

    counts: dict[str, tuple[int, int]] = {}
    checks: dict[str, bool] = {}
    errors: list[str] = []
    wallets: list[WalletCheck] = []
    with TemporaryDirectory(prefix="verify-backup-", dir=target.parent) as directory:
        reference = Path(directory) / "reference.sqlite3"
        restore(source, reference)
        with closing(open_legacy(reference)) as old, closing(open_legacy(target)) as new:
            old.execute("BEGIN")
            new.execute("BEGIN")
            for (table,) in old.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
                columns = [row[1] for row in old.execute(f"PRAGMA table_info({quote(table)})")]
                # Application times for newly applied migrations differ per copy;
                # the immutable migration identity and checksum must match.
                if table == "schema_migration":
                    columns = ["id", "checksum"]
                fields = ",".join(map(quote, columns))
                query = f"SELECT {fields} FROM {quote(table)} ORDER BY {fields}"
                before = old.execute(f"SELECT COUNT(*) FROM {quote(table)}").fetchone()[0]
                after = new.execute(f"SELECT COUNT(*) FROM {quote(table)}").fetchone()[0]
                counts[table] = before, after
                same = before == after and all(
                    a is not None and b is not None and tuple(a) == tuple(b)
                    for a, b in zip_longest(old.execute(query), new.execute(query))
                )
                checks[table] = same
                if not same:
                    errors.append(f"备份恢复后 {table} 内容不一致")
            checks["integrity"] = new.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            checks["foreign_keys"] = not new.execute("PRAGMA foreign_key_check").fetchall()
        with (
            closing(open_connection(reference, query_only=True)) as old,
            closing(open_connection(target, query_only=True)) as new,
        ):
            old_total, new_total = total(old), total(new)
            old_wallet, new_wallet = WalletRepository(old), WalletRepository(new)
            for vendor in old_wallet.list_vendors(include_archived=True):
                before, after = old_wallet.totals(vendor.id), new_wallet.totals(vendor.id)
                wallets.append(
                    WalletCheck(
                        vendor.id,
                        vendor.name,
                        before.b,
                        after.b,
                        after.r,
                        after.c,
                        after.a,
                        "",
                        before.coverage_incomplete,
                        (),
                    )
                )
            metadata = dict(old.execute("SELECT key,value FROM meta"))
    return Verification(
        source,
        target,
        metadata.get("created_by_version", "0.4.0") + " 账本备份",
        sha256_file(source),
        checks,
        counts,
        old_total,
        new_total,
        {},
        tuple(wallets),
        tuple(errors),
        ("完整恢复账本、汇总、价格、钱包与设置。远程账户凭据仍使用本机凭据库。",),
        (),
        (),
        (),
    )
