"""Compare every preserved record, resolved price, total, and wallet against its snapshot."""

import json
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from htd.adapters.sqlite.connection import open_connection
from htd.adapters.sqlite.models_repo import ModelsRepository
from htd.adapters.sqlite.pricing_load import load_catalog
from htd.adapters.sqlite.usage_codec import dumps
from htd.adapters.sqlite.wallet_store import WalletStore, decode_entry
from htd.domain.billing.estimate import estimate_cost
from htd.domain.common.errors import DomainError
from htd.domain.common.money import parse_usd_amount
from htd.domain.common.time import format_instant, parse_instant
from htd.domain.pricing.catalog import Catalog, Resolution
from htd.domain.usage.model import Harness, Tokens
from htd.domain.wallet.ledger import apply
from htd.migration.convert import Conversion, prepare
from htd.migration.legacy.reader import SQLiteConnection
from htd.migration.native import NativeConversion, is_native, verify_native
from htd.migration.preferences import preference_warnings, preferences
from htd.migration.pricing_convert import model, price_mapping


@dataclass(frozen=True, slots=True)
class WalletCheck:
    vendor_id: str
    name: str
    old: int
    new: int
    r: int
    c: int
    a: int
    coverage_start: str
    incomplete_history: bool
    group_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Verification:
    source: Path
    target: Path
    source_version: str
    sha256: str
    checks: dict[str, bool]
    counts: dict[str, tuple[int, int]]
    old_total: int
    new_total: int
    daily: dict[str, tuple[int, int]]
    wallets: tuple[WalletCheck, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    dropped_checkpoints: tuple[str, ...]
    price_mismatches: tuple[str, ...]
    price_mapping: tuple[dict[str, Any], ...]

    @property
    def ok(self) -> bool:
        return all(self.checks.values()) and not self.errors


def _records(db: SQLiteConnection, table: str) -> tuple[dict[str, Any], ...]:
    return tuple(dict(row) for row in db.execute(f"SELECT * FROM {table}"))


def _same(old: tuple[dict[str, Any], ...], new: tuple[dict[str, Any], ...]) -> bool:
    return sorted(dumps(row) for row in old) == sorted(dumps(row) for row in new)


def _daily(rows: tuple[dict[str, Any], ...], amount: str) -> dict[str, int]:
    values: defaultdict[str, int] = defaultdict(int)
    for row in rows:
        values[f"{row['harness']}:{row['occurred_at'][:10]}"] += int(row[amount])
    return dict(values)


def _recorded_price_matches(catalog: Catalog, row: dict[str, Any]) -> bool:
    """Legacy supplier routing can legitimately override the selection timeline.

    Accept its recorded choice only when its group, version, exact cost and
    quality flags can all be independently reproduced from the preserved rates.
    """
    if row["price_group_id"] is None or row["price_version_id"] is None:
        return False
    version = catalog.version(row["price_version_id"])
    if version.group_id != row["price_group_id"]:
        return False
    at = parse_instant(row["occurred_at"])
    revision = catalog.revision_at(version.id, at)
    resolved = Resolution(catalog.group(version.group_id), version, revision)
    tokens = Tokens(
        **{key: row[key + "_tokens"] for key in ("input", "cache_read", "cache_write", "output", "reasoning")}
    )
    cost = estimate_cost(tokens, resolved.rate_for(row["model_id"]), at)
    return (cost.pico_usd, cost.incomplete, cost.unpriced) == (
        int(row["pico_usd"]),
        bool(row["incomplete"]),
        bool(row["unpriced"]),
    )


def _derived_warnings(conversion: Conversion) -> list[str]:
    source = conversion.source
    catalog = conversion.catalog
    expected_groups = {(g.id, g.name, int(g.disabled)) for g in catalog.groups}
    actual_groups = {(r["id"], r["name"], r["disabled"]) for r in source.rows("price_groups")}
    warnings: list[str] = []
    if source.rows("price_groups") and expected_groups != actual_groups:
        warnings.append("旧 price_groups 派生副本与配置 JSON 不一致，以 JSON 为准")
    expected_versions = {(v.id, v.group_id, v.number, format_instant(v.created_at)) for v in catalog.versions}
    actual_versions = {
        (r["id"], r["group_id"], r["number"], format_instant(parse_instant(r["effective_at"])))
        for r in source.rows("price_versions")
    }
    if source.rows("price_versions") and expected_versions != actual_versions:
        warnings.append("旧 price_versions 派生副本与配置 JSON 不一致，以 JSON 为准")
    labels = {version.id: version.label for version in catalog.versions}
    if any("label" in row and row["label"] != labels.get(row["id"]) for row in source.rows("price_versions")):
        warnings.append("旧 price_versions label 与配置 JSON 不一致，以 JSON 为准")
    try:
        actual_rates: dict[str, list[dict[str, Any]]] = {}
        for row in source.rows("price_model_rates"):
            actual_rates.setdefault(row["version_id"], []).append(
                {
                    "modelId": row["model_id"],
                    "displayName": row["display_name"],
                    "input": row.get("input_rate", row.get("input")),
                    "cache": row.get("cache_rate", row.get("cache")),
                    "output": row.get("output_rate", row.get("output")),
                    "billing": json.loads(row["billing_json"]) if row["billing_json"] else None,
                }
            )
        for version in catalog.versions:
            expected = catalog.revision_at(version.id, catalog.latest_time or conversion.at).models
            actual = tuple(
                sorted((model(rate) for rate in actual_rates.get(version.id, [])), key=lambda rate: rate.key)
            )
            if actual != expected:
                warnings.append("旧 price_model_rates 派生副本与配置 JSON 不一致，以 JSON 为准")
                break
        for table, expected_selections in (
            (
                "price_selections",
                source.state("configuration").get("selections", [])
                + (
                    [
                        {"harness": "dsh", "groupId": r["group_id"], "effectiveAt": r["effective_at"]}
                        for r in source.rows("price_selections")
                        if r["harness"] == "dsh"
                    ]
                    if source.identity.user_version == 7
                    else []
                ),
            ),
            ("dsh_price_selections", conversion.merged.extension.get("dshSelections", [])),
        ):
            expected = [
                (r["harness"], r["groupId"], format_instant(parse_instant(r["effectiveAt"])))
                for r in expected_selections
            ]
            actual = [
                (r.get("harness", "dsh"), r["group_id"], format_instant(parse_instant(r["effective_at"])))
                for r in source.rows(table)
            ]
            if expected != actual:
                warnings.append(f"旧 {table} 派生副本与配置 JSON 不一致，以 JSON 为准")
    except (DomainError, KeyError, TypeError, ValueError):
        warnings.append("旧价格派生副本无效，以配置 JSON 为准")
    return warnings


def verify(
    source: Path, target: Path, *, conversion: Conversion | NativeConversion | None = None
) -> Verification:
    if isinstance(conversion, NativeConversion) or (conversion is None and is_native(source)):
        return verify_native(source, target)
    prepared = conversion if isinstance(conversion, Conversion) else prepare(source)
    checks: dict[str, bool] = {}
    counts: dict[str, tuple[int, int]] = {}
    errors: list[str] = []
    warnings = _derived_warnings(prepared)
    warnings.extend(preference_warnings(prepared.merged))
    if prepared.records.dropped_checkpoints:
        warnings.append(f"丢弃 {len(prepared.records.dropped_checkpoints)} 个无法解析的检查点，保留身份去重")
    wallet_checks: list[WalletCheck] = []
    mismatches: list[str] = []
    old_rows = prepared.source.rows("usage_ledger")
    batches = {row["id"]: row for row in prepared.source.rows("usage_recalc_batches")}
    recalculated: dict[tuple[str, str], dict[str, Any]] = {}
    for item in sorted(
        prepared.source.rows("usage_recalc_items"),
        key=lambda item: (batches[item["batch_id"]]["committed_at"], item["batch_id"]),
    ):
        recalculated[item["harness"], item["dedupe_key"]] = item
    audited_overrides = 0
    recorded_overrides = 0
    old_total = sum(int(row["pico_usd"]) for row in old_rows)
    new_total = 0
    daily: dict[str, tuple[int, int]] = {}
    if not target.is_file():
        raise DomainError("MIGRATION_TARGET_INVALID", "待校验的新库不存在")
    with closing(open_connection(target, query_only=True)) as db:
        db.execute("BEGIN")
        try:
            new_rows = _records(db, "ledger")
            new_total = sum(int(row["pico"]) for row in new_rows)
            old_daily, new_daily = _daily(old_rows, "pico_usd"), _daily(new_rows, "pico")
            daily = {
                key: (old_daily.get(key, 0), new_daily.get(key, 0))
                for key in sorted(old_daily.keys() | new_daily.keys())
            }
            checks["汇总金额与 Harness x UTC 日期"] = old_total == new_total and old_daily == new_daily
            for table, expected in prepared.records.tables.items():
                actual = _records(db, table)
                if table == "runtime_state":
                    expected_keys = {row["key"] for row in expected}
                    actual = tuple(row for row in actual if row["key"] in expected_keys)
                if expected:
                    fields = set(expected[0])
                    actual = tuple({key: row[key] for key in fields} for row in actual)
                counts[table] = (len(expected), len(actual))
                checks[f"行数: {table}"] = len(expected) == len(actual)
                checks[f"逐行: {table}"] = _same(expected, actual)
            actual_catalog = load_catalog(db)
            for name in ("groups", "versions", "revisions", "activations", "selections", "hints"):
                checks["价格: " + name] = getattr(prepared.catalog, name) == getattr(actual_catalog, name)
            checks["模型池、排除名单与基准价"] = ModelsRepository(db).load() == prepared.models
            for row in old_rows:
                resolution = actual_catalog.resolve(
                    Harness(row["harness"]), parse_instant(row["occurred_at"])
                )
                resolved = (
                    resolution.group.id if resolution.group else None,
                    resolution.version.id if resolution.version else None,
                )
                recorded = (row["price_group_id"], row["price_version_id"])
                if resolved != recorded:
                    item = recalculated.get((row["harness"], row["dedupe_key"]))
                    if item is not None:
                        batch = batches[item["batch_id"]]
                        version = actual_catalog.version(batch["version_id"])
                        supported = all(
                            item["new_" + key] == row[key]
                            for key in (
                                "price_group_id",
                                "price_version_id",
                                "pico_usd",
                                "incomplete",
                                "unpriced",
                            )
                        )
                        if (
                            supported
                            and recorded == (batch["group_id"], version.id)
                            and (
                                version.group_id == batch["group_id"]
                                and batch["harness"] == row["harness"]
                                and parse_instant(batch["start_at"])
                                <= parse_instant(row["occurred_at"])
                                < parse_instant(batch["end_at"])
                            )
                        ):
                            actual_catalog.revision_at(version.id, parse_instant(row["occurred_at"]))
                            audited_overrides += 1
                            continue
                    elif _recorded_price_matches(actual_catalog, row):
                        recorded_overrides += 1
                        continue
                    mismatches.append(
                        f"{row['harness']}:{row['dedupe_key']}: 旧 {recorded} / 新解析 {resolved}"
                    )
            checks["逐条账本价格解析"] = not mismatches
            actual_prefs = {row["key"]: json.loads(row["value_json"]) for row in _records(db, "preference")}
            checks["偏好及原始筛选字符串"] = actual_prefs == (
                {
                    "accountingTimezone": "Asia/Shanghai",
                    "unpricedNoticeDismissed": "",
                    "longTermRetention": "forever",
                }
                | preferences(prepared.merged)
            )
            checks["供应商行数"] = (
                len(prepared.wallets) == db.execute("SELECT COUNT(*) FROM vendor").fetchone()[0]
            )
            checks["钱包凭证行数"] = (
                sum(len(plan.entries) for plan in prepared.wallets)
                == db.execute("SELECT COUNT(*) FROM wallet_entry").fetchone()[0]
            )
            checks["钱包累计行数"] = (
                len(prepared.wallets) == db.execute("SELECT COUNT(*) FROM wallet_total").fetchone()[0]
            )
            expected_links: list[tuple[str, str, str]] = []
            expected_audits: list[tuple[str, str, str, str]] = []
            state = {row["key"]: row["value"] for row in _records(db, "runtime_state")}
            for plan in prepared.wallets:
                vendor = plan.vendor
                rows = tuple(
                    db.execute("SELECT * FROM wallet_entry WHERE vendor_id=? ORDER BY seq", (vendor.id,))
                )
                entries = tuple(decode_entry(row) for row in rows)
                totals = apply(entries)
                cache = WalletStore(db).totals(vendor.id)
                checks[f"钱包: {vendor.id}"] = (
                    entries == plan.entries
                    and totals.b == plan.old_balance
                    and (totals.r, totals.c, totals.a) == (cache.r, cache.c, cache.a)
                )
                last_seq = db.execute(
                    "SELECT last_seq FROM wallet_total WHERE vendor_id=?", (vendor.id,)
                ).fetchone()[0]
                checks[f"钱包水位: {vendor.id}"] = last_seq == max(entry.seq for entry in plan.entries)
                coverage = state.get("history_coverage_start:" + vendor.id)
                checks[f"历史覆盖: {vendor.id}"] = coverage == format_instant(
                    plan.coverage_start
                ) and state.get("history_incomplete:" + vendor.id) == (
                    "true" if plan.incomplete_history else "false"
                )
                vendor_row = db.execute("SELECT * FROM vendor WHERE id=?", (vendor.id,)).fetchone()
                checks[f"供应商: {vendor.id}"] = vendor_row is not None and (
                    vendor_row["name"],
                    vendor_row["created_at"],
                    vendor_row["status"],
                ) == (vendor.name, format_instant(vendor.created_at), vendor.status)
                expected_links.extend(
                    (group, vendor.id, format_instant(vendor.created_at)) for group in plan.group_ids
                )
                expected_audits.extend(
                    (
                        vendor.id,
                        format_instant(parse_instant(audit["at"])),
                        str(parse_usd_amount(audit["balance"], allow_negative=True)),
                        dumps(audit),
                    )
                    for audit in plan.audits
                )
                wallet_checks.append(
                    WalletCheck(
                        vendor.id,
                        vendor.name,
                        plan.old_balance,
                        totals.b,
                        totals.r,
                        totals.c,
                        totals.a,
                        format_instant(plan.coverage_start),
                        plan.incomplete_history,
                        plan.group_ids,
                    )
                )
            checks["钱包关联时间线"] = sorted(expected_links) == sorted(
                tuple(row) for row in db.execute("SELECT group_id,vendor_id,effective_at FROM vendor_link")
            )
            checks["旧校准审计"] = sorted(expected_audits) == sorted(
                tuple(row)
                for row in db.execute("SELECT vendor_id,at,amount_pico,source_json FROM vendor_legacy_record")
            )
            meta = dict(db.execute("SELECT key,value FROM meta"))
            checks["快照 SHA-256"] = meta.get("legacy_snapshot_sha256") == prepared.sha256
            checks["价格原串与规范形式对照"] = meta.get("legacy_price_mapping") == dumps(
                price_mapping(prepared.merged, prepared.catalog)
            )
            checks["旧保留清理审计"] = meta.get("legacy_retention_runs") == dumps(
                prepared.source.rows("retention_runs")
            )
            checks["foreign_key_check"] = not db.execute("PRAGMA foreign_key_check").fetchall()
            checks["integrity_check"] = [row[0] for row in db.execute("PRAGMA integrity_check")] == ["ok"]
        except Exception as error:
            checks["新库读取与数据格式"] = False
            errors.append(str(error))
        finally:
            db.rollback()
    errors.extend(name for name, passed in checks.items() if not passed)
    if audited_overrides:
        warnings.append(f"{audited_overrides} 条账本使用旧版手动重算版本，已核对最新批次、范围及逐项新状态")
    if recorded_overrides:
        warnings.append(
            f"{recorded_overrides} 条账本使用不同于选择时间线的旧版价格，已按记录的分组和版本"
            "逐条重算核对精确金额与质量标记，保留原费用，不改写价格选择时间线"
        )
    return Verification(
        source.resolve(),
        target.resolve(),
        prepared.source.identity.version,
        prepared.sha256,
        checks,
        counts,
        old_total,
        new_total,
        daily,
        tuple(wallet_checks),
        tuple(errors),
        tuple(warnings),
        prepared.records.dropped_checkpoints,
        tuple(mismatches),
        price_mapping(prepared.merged, prepared.catalog),
    )
