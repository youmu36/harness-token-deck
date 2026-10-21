"""Read details and one historical authority; price-dimensional copies split old totals."""

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.counter_codec import decompress
from htd.adapters.sqlite.explorer_storage import detail
from htd.adapters.sqlite.usage_archive import archived_rows
from htd.adapters.sqlite.usage_daily import rows as daily_rows
from htd.adapters.sqlite.wallet_closing import timezone
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.statistics.explorer import ExploreTotals


def options(db: sqlite3.Connection) -> dict[str, object]:
    from htd.adapters.sqlite.ledger_read import LedgerReader
    from htd.domain.usage.model_key import model_key

    return {
        "vendors": [
            {"id": r["id"], "name": r["name"] + (" (归档)" if r["status"] == "archived" else "")}
            for r in db.execute("SELECT id,name,status FROM vendor ORDER BY name")
        ],
        "groups": [
            {"id": r["id"], "name": r["name"]}
            for r in db.execute("SELECT id,name FROM price_group ORDER BY seq")
        ],
        "versions": [
            {"id": r["id"], "name": r["label"] or f"版本 {r['number']}", "parentId": r["group_id"]}
            for r in db.execute(
                "SELECT v.*,COALESCE(l.label,'') label FROM price_version v LEFT JOIN "
                "price_version_label l ON l.version_id=v.id ORDER BY v.seq"
            )
        ],
        "models": [{"id": model_key(name), "name": name} for name in LedgerReader(db).observed_models()][
            :5000
        ],
    }


def explore(db: sqlite3.Connection, query: dict[str, Any], at: Instant) -> dict[str, object]:
    start, end = parse_instant(query["start"]), parse_instant(query["end"])
    if end <= start or end.value - start.value > timedelta(days=36600):
        raise DomainError("RANGE_INVALID", "请选择有效且不超过一百年的统计范围")
    zone_name = timezone(db)
    zone = ZoneInfo(zone_name)
    grain = query["grain"]
    notices: set[str] = set()
    if grain == "hour" and end.value - start.value > timedelta(days=7):
        grain = "day"
        notices.add("超过七天按天展示，小时明细可缩小范围后查看。")
    if grain == "day" and end.value - start.value > timedelta(days=120):
        grain = "month"
    if grain == "month" and end.value - start.value > timedelta(days=3652):
        grain = "year"
    # Old dimensional rows use months; include whole boundary months consistently.
    old_year = at.value.astimezone(zone).year - 2
    local_start, local_end = start.value.astimezone(zone), end.value.astimezone(zone)
    if local_start.year < old_year:
        expanded = local_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if expanded != local_start:
            start = Instant(expanded)
            notices.add("较早历史按月合并，包含完整边界月份。")
    if local_end.year < old_year and (
        local_end.day != 1 or local_end.hour or local_end.minute or local_end.second or local_end.microsecond
    ):
        end = Instant(
            (
                local_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=32)
            ).replace(day=1)
        )
        notices.add("较早历史按月合并，包含完整边界月份。")
    local_start, local_end = start.value.astimezone(zone), end.value.astimezone(zone)
    begin, finish = format_instant(start), format_instant(end)
    total = ExploreTotals()
    models: dict[str, tuple[str, ExploreTotals]] = {}
    trend: dict[str, ExploreTotals] = {}
    groups: dict[tuple[str, ...], tuple[str, ExploreTotals]] = {}
    known: dict[tuple[str, ...], ExploreTotals] = {}
    legacy: dict[tuple[str, ...], tuple[str, str, ExploreTotals]] = {}
    unknown_records = 0
    quality: dict[tuple[str, ...], int] = {}
    names = ("harnesses", "vendorIds", "groupIds", "versionIds", "modelKeys")
    selected = [set(query[name]) for name in names]

    def check_budget() -> None:
        estimated = (
            len(models) * 700
            + len(groups) * 1000
            + len(known) * 900
            + len(legacy) * 900
            + len(quality) * 400
            + len(trend) * 700
        )
        if estimated > 16 * 1024**2:
            raise DomainError("ANALYSIS_LIMIT", "分析已达内存预算，请缩小范围或筛选模型")

    def accepts(key: tuple[str, ...], base: bool = False) -> bool:
        return all(
            not choices or value in choices
            for i, (value, choices) in enumerate(zip(key, selected, strict=True))
            if not base or i not in (2, 3)
        )

    def history_key(stamp: str, key: tuple[str, ...]) -> tuple[str, ...]:
        local = parse_instant(stamp).value.astimezone(zone)
        return (local.strftime("%Y-%m" if local.year < old_year else "%Y-%m-%d"), key[0], key[1], key[4])

    def accept(
        stamp: str, key: tuple[str, ...], name: str, values: dict[str, int], coarse: bool = False
    ) -> None:
        nonlocal unknown_records
        if not accepts(key) or not values["records"]:
            return
        if len(models) >= 5000 and key[4] not in models:
            raise DomainError("ANALYSIS_LIMIT", "模型分组超过限制，请缩小范围")
        if len(groups) >= 10000 and key not in groups:
            raise DomainError("ANALYSIS_LIMIT", "分析维度超过限制，请缩小范围")
        local = parse_instant(stamp).value.astimezone(zone)
        bucket = local.strftime(
            "%Y"
            if grain == "year"
            else "%Y-%m"
            if grain == "month"
            else "%Y-%m-%d %H:00"
            if grain == "hour" and not coarse
            else "%Y-%m-%d"
        )
        check_budget()
        total.add(values)
        models.setdefault(key[4], (name, ExploreTotals()))[1].add(values)
        trend.setdefault(bucket, ExploreTotals()).add(values)
        groups.setdefault(key, (name, ExploreTotals()))[1].add(values)
        reasons: list[str] = []
        if not key[3]:
            reasons.append("价格组/倍率版本缺失")
        if values["unknown"]:
            reasons.append("缓存覆盖不完整")
        if values["incomplete"]:
            reasons.append("用量或计费字段不完整")
        if values["unpriced"]:
            reasons.append("未定价")
        if reasons:
            qdate = local.strftime("%Y-%m" if local.year < old_year else "%Y-%m-%d")
            qkey = (qdate, *key, name, *reasons)
            quality[qkey] = quality.get(qkey, 0) + values["records"]
        if not key[3]:
            unknown_records += values["records"]
        if coarse and grain == "hour":
            notices.add("较早记录只保留日/月汇总，未伪造小时明细。")

    clause = ""
    args: list[str] = []
    for column, choices in (
        ("l.harness", selected[0]),
        ("COALESCE(w.vendor_id,'')", selected[1]),
        ("l.price_group_id", selected[2]),
        ("l.price_version_id", selected[3]),
        ("l.model_key", selected[4]),
    ):
        if choices:
            clause += f" AND COALESCE({column},'') IN ({','.join('?' for _ in choices)})"
            args.extend(sorted(choices))
    for row in db.execute(
        "SELECT l.*,COALESCE(w.vendor_id,'') vendor_id,a.cache_read detected_cache FROM ledger l "
        "LEFT JOIN usage_annotation a USING(harness,dedupe_key) LEFT JOIN wallet_entry w ON "
        "w.usage_harness=l.harness AND w.usage_key=l.dedupe_key AND w.kind='consumption' "
        "WHERE l.occurred_at>=? AND l.occurred_at<?" + clause,
        (begin, finish, *args),
    ):
        key = (
            row["harness"],
            row["vendor_id"],
            row["price_group_id"] or "",
            row["price_version_id"] or "",
            row["model_key"],
        )
        accept(row["occurred_at"], key, row["model_id"] or "模型未知", detail(db, dict(row)))

    # Retired copies preserve new dimensions; they are subtracted from the old
    # minute/day authority before accepting its legacy, unknown-version remainder.
    for row in db.execute("SELECT * FROM usage_dimensions WHERE end_at>? AND bucket<?", (begin, finish)):
        key = (row["harness"], row["vendor_id"], row["group_id"], row["version_id"], row["model_key"])
        if not accepts(key, base=True):
            continue
        values = decompress(row["payload"])
        known.setdefault(history_key(row["bucket"], key), ExploreTotals()).add(values)
        check_budget()
        accept(row["bucket"], key, row["model_id"], values, True)
        if row["bucket"] < begin or row["end_at"] > finish:
            notices.add("已合并历史包含完整边界业务日或月份。")

    def historic(rows: Iterable[dict[str, Any]]) -> None:
        for row in rows:
            key = (row["harness"], row["vendor_id"], "", "", row["model_key"])
            if not accepts(key, base=True):
                continue
            values = {
                name: int(row[field])
                for name, field in (
                    ("records", "records"),
                    ("input", "input"),
                    ("cache", "cache_read"),
                    ("output", "output"),
                    ("write", "cache_write"),
                    ("pico", "pico"),
                    ("rated_input", "rated_input"),
                    ("rated_cache", "rated_cache"),
                    ("unknown", "unknown_cache"),
                    ("incomplete", "incomplete"),
                    ("unpriced", "unpriced"),
                )
            }
            identity = history_key(row["bucket"], key)
            legacy.setdefault(identity, (row["bucket"], row["model_id"] or "模型未知", ExploreTotals()))[
                2
            ].add(values)
            check_budget()
            if len(legacy) > 10000:
                raise DomainError("ANALYSIS_LIMIT", "历史聚合达到预算，请缩小范围")

    # Version-only queries do not decode unrelated old archive blocks.
    if (not selected[2] or "" in selected[2]) and (not selected[3] or "" in selected[3]):
        # Full business boundary days are necessary to subtract dimensional days.
        day_start = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = local_end.replace(hour=0, minute=0, second=0, microsecond=0)
        if local_end != day_end:
            day_end += timedelta(days=1)
        hbegin, hend = format_instant(Instant(day_start)), format_instant(Instant(day_end))
        historic(
            dict(r)
            for r in db.execute("SELECT * FROM usage_rollup WHERE bucket>=? AND bucket<?", (hbegin, hend))
        )
        historic(archived_rows(db, hbegin, hend))
        historic(counter for _, counter in daily_rows(db, hbegin, hend))
        for identity, (stamp, name, counters) in legacy.items():
            if identity in known:
                # Historical quality booleans are OR flags, not subtractable counts.
                subtract = {
                    k: v
                    for k, v in known[identity].values.items()
                    if k not in ("incomplete", "unpriced") and not k.startswith("eligible_")
                }
                counters.add(subtract, -1)
            if counters.values["records"] < 0:
                raise DomainError("ANALYSIS_INVALID", "历史维度与权威汇总不一致")
            if counters.values["records"]:
                accept(stamp, (identity[1], identity[2], "", "", identity[3]), name, counters.values, True)
                notices.add("旧版历史缺少价格组/倍率版本，未推测归属，每美元计算仅采用证据完整样本。")
        if legacy and (begin != hbegin or finish != hend):
            notices.add("已合并历史包含完整边界业务日，近期明细仍按所选时间。")
    elif any(selected[2:4]):
        notices.add("价格组/倍率筛选仅包含可确认历史归属的记录，缺失版本的旧记录不计入。")
    ranked = sorted(models.items(), key=lambda item: (-item[1][1].values["pico"], item[0]))
    offset, limit = query["offset"], query["limit"]
    issues: list[dict[str, Any]] = []
    for key, count in sorted(quality.items(), reverse=True):
        identifier = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()
        fingerprint = hashlib.sha256(f"{identifier}:{count}".encode()).hexdigest()
        issues.append(
            {
                "id": identifier,
                "fingerprint": fingerprint,
                "date": key[0],
                "harness": key[1],
                "vendorId": key[2],
                "groupId": key[3],
                "versionId": key[4],
                "modelKey": key[5],
                "displayName": key[6],
                "reasons": list(key[7:]),
                "recordCount": str(count),
                "acknowledged": False,
            }
        )
    # Account differences are evidence of a reconciliation gap, not model tokens.
    # They are only shown for account-wide scopes; never allocate them to a model.
    if not any(selected[index] for index in (0, 2, 3, 4)):
        for row in db.execute(
            "SELECT i.id,i.e,i.c,s.vendor_id,t.observed_at FROM account_interval i "
            "JOIN account_segment s ON s.id=i.segment_id JOIN account_endpoint t ON t.id=i.end_endpoint "
            "WHERE t.observed_at>=? AND t.observed_at<? ORDER BY t.observed_at DESC LIMIT 1000",
            (begin, finish),
        ):
            if selected[1] and row["vendor_id"] not in selected[1]:
                continue
            difference = int(row["c"]) - int(row["e"])
            if difference <= 0:
                continue
            identifier = hashlib.sha256(f"account-gap:{row['id']}".encode()).hexdigest()
            fingerprint = hashlib.sha256(f"{identifier}:{row['c']}:{row['e']}".encode()).hexdigest()
            issues.append(
                {
                    "id": identifier,
                    "fingerprint": fingerprint,
                    "date": parse_instant(row["observed_at"]).value.astimezone(zone).date().isoformat(),
                    "harness": "账户级",
                    "vendorId": row["vendor_id"],
                    "groupId": "",
                    "versionId": "",
                    "modelKey": "",
                    "displayName": "账户区间对照",
                    "reasons": ["远程实扣高于区间本地估算，原因待核对"],
                    "recordCount": "1",
                    "costDifferencePico": str(difference),
                    "acknowledged": False,
                }
            )
        issues.sort(key=lambda item: (item["date"], item["id"]), reverse=True)
    acknowledgements: dict[str, str] = {}
    identifiers = [item["id"] for item in issues]
    for index in range(0, len(identifiers), 500):
        batch = identifiers[index : index + 500]
        acknowledgements.update(
            db.execute(
                "SELECT id,fingerprint FROM quality_notice_ack WHERE id IN ("
                + ",".join("?" for _ in batch)
                + ")",
                batch,
            )
        )
    for item in issues:
        item["acknowledged"] = acknowledgements.get(item["id"]) == item["fingerprint"]
    qoffset = query.get("qualityOffset", 0)
    return {
        "start": begin,
        "end": finish,
        "generatedAt": format_instant(at),
        "businessTimezone": zone_name,
        "grain": grain,
        "totals": total.export(),
        "models": [
            {"modelKey": key, "displayName": name, **value.export()}
            for key, (name, value) in ranked[offset : offset + limit]
        ],
        "totalModels": len(models),
        "nextOffset": offset + limit if offset + limit < len(models) else None,
        "trend": [{"bucket": key, "label": key, **value.export()} for key, value in sorted(trend.items())],
        "breakdowns": [
            {
                "harness": key[0],
                "vendorId": key[1],
                "groupId": key[2],
                "versionId": key[3],
                "modelKey": key[4],
                "displayName": name,
                **value.export(),
            }
            for key, (name, value) in sorted(groups.items(), key=lambda item: -item[1][1].values["pico"])[
                :100
            ]
        ],
        "missingVersionRecords": str(unknown_records),
        "notices": sorted(notices),
        "revision": db.execute("SELECT value FROM revision WHERE scope='usage'").fetchone()[0],
        "quality": issues[qoffset : qoffset + 100],
        "qualityTotal": len(issues),
        "qualityUnacknowledged": sum(not item["acknowledged"] for item in issues),
        "qualityNextOffset": qoffset + 100 if qoffset + 100 < len(issues) else None,
    }
