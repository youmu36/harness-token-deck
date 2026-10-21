"""Build a deterministic staging database without changing the active or legacy database."""

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from htd.adapters.sqlite.connection import open_connection
from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.migrate import migrate
from htd.adapters.sqlite.models_repo import ModelsRepository
from htd.adapters.sqlite.pricing_repo import PricingRepository
from htd.adapters.sqlite.usage_codec import dumps
from htd.adapters.sqlite.writer import UnitOfWork
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant
from htd.domain.pricing.catalog import Catalog
from htd.domain.pricing.model_pool import ModelPool
from htd.migration.legacy.merge import LegacyMerged, merge_legacy
from htd.migration.legacy.reader import LegacySnapshot, SQLiteConnection, read_legacy, require_legacy_stopped
from htd.migration.legacy.records import MappedRecords, map_records
from htd.migration.native import NativeConversion, is_native, restore
from htd.migration.preferences import preferences
from htd.migration.pricing_convert import catalog_from_legacy, models_from_legacy, price_mapping
from htd.migration.snapshot import sha256_file, snapshot_time
from htd.migration.wallet_convert import WalletPlan, convert_wallet, plan_wallets, stable_id


@dataclass(frozen=True, slots=True)
class Conversion:
    source: LegacySnapshot
    merged: LegacyMerged
    catalog: Catalog
    models: ModelPool
    records: MappedRecords
    wallets: tuple[WalletPlan, ...]
    at: Instant
    sha256: str


def prepare(path: Path) -> Conversion:
    source = read_legacy(path)
    at = snapshot_time(path, source.rows("configuration")[0]["updated_at"])
    merged = merge_legacy(source, at=at)
    catalog = catalog_from_legacy(merged, at=at)
    models = models_from_legacy(merged)
    preferences(merged)
    records = map_records(source, catalog, at=at)
    incomplete = any(
        row.get("deleted_ledger", row.get("deleted_ledger_rows", 0)) > 0
        for row in source.rows("retention_runs")
    )
    wallets = plan_wallets(merged, source.rows("usage_ledger"), at=at, incomplete_history=incomplete)
    return Conversion(source, merged, catalog, models, records, wallets, at, sha256_file(path))


def require_empty_target(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    with closing(open_connection(path, query_only=True)) as db:
        tables = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        app_id = (
            db.execute("SELECT value FROM meta WHERE key='app_id'").fetchone() if "meta" in tables else None
        )
        if app_id is None or app_id[0] != "com.harness-token-deck.ledger":
            raise DomainError("MIGRATION_TARGET_INVALID", "目标不是新版账本，拒绝覆盖")
        for table in (
            "ledger",
            "wallet_entry",
            "usage_rollup",
            "usage_archive",
            "wallet_rollup",
            "wallet_archive",
        ):
            if table in tables and db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
                raise DomainError("MIGRATION_TARGET_NOT_EMPTY", "新版已有账本或钱包凭证，不能自动导入")


def insert_rows(db: SQLiteConnection, table: str, rows: tuple[dict[str, Any], ...]) -> None:
    for row in rows:
        keys = tuple(row)
        db.execute(
            f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})"
            + (
                " ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json"
                if table == "preference"
                else ""
            ),
            tuple(
                key_bytes(row[key])
                if key in ("dedupe_key", "usage_key") and row[key] is not None
                else row[key]
                for key in keys
            ),
        )


def convert(
    path: Path, staging: Path, *, target: Path | None = None, detector: Callable[[], bool] | None = None
) -> Conversion | NativeConversion:
    require_legacy_stopped(detector=detector)
    path, staging = path.resolve(), staging.resolve()
    if target is not None:
        require_empty_target(target)
    if staging == path or staging.exists():
        if staging != path:
            require_empty_target(staging)
        raise DomainError("MIGRATION_STAGING_EXISTS", "staging 已存在，请选择新的迁移目录")
    native = is_native(path)
    prepared = None if native else prepare(path)
    staging.parent.mkdir(parents=True, exist_ok=True)
    with staging.open("xb"):
        pass
    if native:
        return restore(path, staging)
    assert prepared is not None
    migrate(staging, now=prepared.at)
    with closing(open_connection(staging)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            work = UnitOfWork(db)
            PricingRepository(db).save(work, Catalog(), prepared.catalog)
            ModelsRepository(db).save(work, ModelPool(), prepared.models, at=prepared.at)
            for table, rows in prepared.records.tables.items():
                insert_rows(db, table, rows)
            convert_wallet(work, prepared.wallets)
            values = preferences(prepared.merged)
            insert_rows(
                db,
                "preference",
                tuple({"key": key, "value_json": dumps(value)} for key, value in values.items()),
            )
            db.execute(
                "INSERT INTO meta VALUES ('legacy_import_id',?)", (stable_id("snapshot", prepared.sha256),)
            )
            db.execute("INSERT INTO meta VALUES ('legacy_snapshot_sha256',?)", (prepared.sha256,))
            db.execute(
                "INSERT INTO meta VALUES ('legacy_price_mapping',?)",
                (dumps(price_mapping(prepared.merged, prepared.catalog)),),
            )
            db.execute(
                "INSERT INTO meta VALUES ('legacy_retention_runs',?)",
                (dumps(prepared.source.rows("retention_runs")),),
            )
            work.touch("preferences")
            if prepared.records.tables["ledger"]:
                work.touch("usage")
            if prepared.records.tables["source"]:
                work.touch("sources")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("PRAGMA journal_mode=DELETE")
    return prepared
