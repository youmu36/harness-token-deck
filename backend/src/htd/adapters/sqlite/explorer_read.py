"""Read details and one historical authority; price-dimensional copies split old totals."""

import sqlite3
from collections.abc import Iterable
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.counter_codec import decompress
from htd.adapters.sqlite.explorer_storage import detail, legacy_values
from htd.adapters.sqlite.quality_projection import REASONS, QualityProjection
from htd.adapters.sqlite.usage_archive import archived_rows
from htd.adapters.sqlite.usage_attribution import VENDOR_SQL
from htd.adapters.sqlite.usage_codec import decode_event
from htd.adapters.sqlite.usage_daily import rows as daily_rows
from htd.adapters.sqlite.wallet_closing import timezone
from htd.application.ports.query_control import check_read
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.statistics.effective_cost import CostProjection
from htd.domain.statistics.explorer import ExploreTotals
from htd.domain.statistics.query import QueryPlan


def explore(db: sqlite3.Connection, query: dict[str, Any], at: Instant) -> dict[str, object]:
    start, end = parse_instant(query["start"]), parse_instant(query["end"])
    if end <= start or end.value - start.value > timedelta(days=36600):
        raise DomainError("RANGE_INVALID", "请选择有效且不超过一百年的统计范围")
    if query.get("costBasis") == "remote":
        from htd.adapters.sqlite.account_analysis import remote_explore

        return remote_explore(db, query, at)
    sections = set(query.get("sections", ["totals", "models", "trend", "breakdowns", "quality"]))
    session_scoped = bool(query.get("sessionRefs") or query.get("activityBatchIds"))

    def membership(alias: str) -> tuple[str, list[str]]:
        clause = ""
        arguments: list[str] = []
        for name, expression in (
            ("sessionRefs", "COALESCE(r.session_ref,'')"),
            ("activityBatchIds", "b.batch_id"),
        ):
            choices = query.get(name, [])
            if choices:
                clause += " AND " + expression + " IN (" + ",".join("?" for _ in choices) + ")"
                arguments.extend(choices)
        return (
            " AND EXISTS(SELECT 1 FROM session_request r LEFT JOIN session_request_batch b "
            "USING(harness,dedupe_key) WHERE r.harness="
            + alias
            + ".harness AND r.dedupe_key="
            + alias
            + ".dedupe_key"
            + clause
            + ")",
            arguments,
        )

    if session_scoped:
        missing_clause, missing_args = membership("s")
        if db.execute(
            "SELECT 1 FROM session_request s WHERE s.state<>'accepted' "
            "AND s.occurred_at>=? AND s.occurred_at<? "
            "AND NOT EXISTS(SELECT 1 FROM ledger l WHERE l.harness=s.harness AND l.dedupe_key=s.dedupe_key) "
            "AND NOT EXISTS(SELECT 1 FROM usage_recent_member m WHERE m.harness=s.harness "
            "AND m.dedupe_key=s.dedupe_key) " + missing_clause + " LIMIT 1",
            (query["start"], query["end"], *missing_args),
        ).fetchone():
            raise DomainError("HISTORY_CAPABILITY", "旧历史缺少可确认的会话分钟，不能返回全量冒充筛选结果")
    zone_name = timezone(db)
    zone = ZoneInfo(zone_name)
    grain = query["grain"]
    notices: set[str] = set()
    minute_start = start.value.replace(second=0, microsecond=0)
    minute_end = end.value.replace(second=0, microsecond=0)
    if minute_end < end.value:
        minute_end += timedelta(minutes=1)
    edge_clause = ""
    edge_args: list[str] = []
    for name, column in (
        ("harnesses", "harness"),
        ("sessionRefs", "session_ref"),
        ("activityBatchIds", "activity_id"),
        ("vendorIds", "vendor_id"),
        ("groupIds", "group_id"),
        ("versionIds", "version_id"),
        ("modelKeys", "model_key"),
    ):
        if choices := query.get(name):
            edge_clause += " AND " + column + " IN (" + ",".join("?" for _ in choices) + ")"
            edge_args.extend(choices)
    if (start.value != minute_start or end.value != minute_end) and db.execute(
        "SELECT 1 FROM usage_recent WHERE ((bucket>=? AND bucket<?) OR (bucket>=? AND bucket<?))"
        + edge_clause
        + " LIMIT 1",
        (
            format_instant(Instant(minute_start)),
            format_instant(start),
            format_instant(Instant(minute_end - timedelta(minutes=1))),
            format_instant(end),
            *edge_args,
        ),
    ).fetchone():
        start, end = Instant(minute_start), Instant(minute_end)
        notices.add("退休分钟包含完整边界分钟，已返回实际范围。")
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
    harness_totals: dict[str, ExploreTotals] = {}
    models: dict[str, tuple[str, ExploreTotals]] = {}
    trend: dict[str, ExploreTotals] = {}
    groups: dict[tuple[str, ...], tuple[str, ExploreTotals]] = {}
    known: dict[tuple[str, ...], ExploreTotals] = {}
    legacy: dict[tuple[str, ...], tuple[str, str, ExploreTotals]] = {}
    unknown_records = 0
    quality = QualityProjection()
    compare_ids = list(dict.fromkeys(query.get("compareIds", [])))
    comparison: dict[str, dict[str, ExploreTotals]] = {key: {} for key in compare_ids}
    compare_index = {"vendor": 1, "group": 2, "version": 3, "model": 4}[
        query.get("compareDimension", "vendor")
    ]
    price_cache: dict[tuple[str, str], Any] = {}
    local_cache: dict[str, Any] = {}

    def local(stamp: str) -> Any:
        if stamp not in local_cache:
            if len(local_cache) >= 2048:
                local_cache.clear()
            local_cache[stamp] = parse_instant(stamp).value.astimezone(zone)
        return local_cache[stamp]

    if (
        grain == "hour"
        and not session_scoped
        and db.execute(
            "SELECT 1 FROM usage_dimensions WHERE end_at>? AND bucket<? LIMIT 1", (begin, finish)
        ).fetchone()
    ):
        grain = "day"
        notices.add("所选范围包含日/月历史，统一按天展示，不伪造小时明细。")
    if (
        grain == "day"
        and not session_scoped
        and db.execute(
            "SELECT 1 FROM usage_dimensions WHERE grain='month' AND end_at>? AND bucket<? LIMIT 1",
            (begin, finish),
        ).fetchone()
    ):
        grain = "month"
        notices.add("所选范围包含按月合并历史，统一按月展示。")
    names = ("harnesses", "vendorIds", "groupIds", "versionIds", "modelKeys")
    selected = [set(query.get(name, [])) for name in names]

    def check_budget() -> None:
        check_read()
        estimated = (
            len(models) * 1500
            + len(groups) * 2200
            + len(known) * 2200
            + len(legacy) * 2200
            + len(quality.groups) * 1800
            + len(trend) * 1500
            + sum(len(points) for points in comparison.values()) * 1500
            + len(price_cache) * 700
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
        date = local(stamp)
        return (date.strftime("%Y-%m" if date.year < old_year else "%Y-%m-%d"), key[0], key[1], key[4])

    def accept(
        stamp: str,
        key: tuple[str, ...],
        name: str,
        values: dict[str, int],
        coarse: bool = False,
        *,
        include_quality: bool = True,
    ) -> None:
        nonlocal unknown_records
        if not accepts(key) or not values["records"]:
            return
        if "models" in sections and len(models) >= 5000 and key[4] not in models:
            raise DomainError("ANALYSIS_LIMIT", "模型分组超过限制，请缩小范围")
        if "breakdowns" in sections and len(groups) >= 10000 and key not in groups:
            raise DomainError("ANALYSIS_LIMIT", "分析维度超过限制，请缩小范围")
        date = local(stamp)
        bucket = date.strftime(
            "%Y"
            if grain == "year"
            else "%Y-%m"
            if grain == "month"
            else "%Y-%m-%d %H:00"
            if grain == "hour" and not coarse
            else "%Y-%m-%d"
        )
        if grain == "hour":
            bucket = format_instant(Instant(date.replace(minute=0, second=0, microsecond=0)))
        check_budget()
        if "totals" in sections:
            harness_totals.setdefault(key[0], ExploreTotals()).add(values)
            total.add(values)
        if "models" in sections:
            models.setdefault(key[4], (name, ExploreTotals()))[1].add(values)
        if "trend" in sections:
            trend.setdefault(bucket, ExploreTotals()).add(values)
        if "breakdowns" in sections:
            groups.setdefault(key, (name, ExploreTotals()))[1].add(values)
        if "comparison" in sections and key[compare_index] in comparison:
            comparison[key[compare_index]].setdefault(bucket, ExploreTotals()).add(values)
        if "quality" in sections and not coarse and include_quality:
            quality.add(date.strftime("%Y-%m-%d"), key, name, values)
        if not key[3]:
            unknown_records += values["records"]
        if coarse and grain == "hour":
            notices.add("较早记录只保留日/月汇总，未伪造小时明细。")

    clause = ""
    args: list[str] = []
    for column, choices in (
        ("l.harness", selected[0]),
        (VENDOR_SQL, selected[1]),
        ("l.price_group_id", selected[2]),
        ("l.price_version_id", selected[3]),
        ("l.model_key", selected[4]),
    ):
        if choices:
            clause += f" AND COALESCE({column},'') IN ({','.join('?' for _ in choices)})"
            args.extend(sorted(choices))
    if session_scoped:
        member_clause, member_args = membership("l")
        clause += member_clause
        args.extend(member_args)
    for row in db.execute(
        f"SELECT l.*,{VENDOR_SQL} vendor_id,a.cache_read detected_cache FROM ledger l "
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
        accept(row["occurred_at"], key, row["model_id"] or "模型未知", detail(db, dict(row), price_cache))

    pending_clause, pending_args = membership("i") if session_scoped else ("", [])
    for row in db.execute(
        "SELECT i.event_json FROM inbox i JOIN session_request s USING(harness,dedupe_key) "
        "WHERE s.state='accepted' AND i.occurred_at>=? AND i.occurred_at<?" + pending_clause,
        (begin, finish, *pending_args),
    ):
        check_read()
        event = decode_event(row[0])
        from htd.domain.usage.model_key import model_key

        key = (event.harness.value, "", "", "", model_key(event.model_id) if event.model_id else "")
        values = detail(
            db,
            {
                "harness": event.harness.value,
                "dedupe_key": event.dedupe_key,
                "model_key": key[4],
                "input": event.tokens.input,
                "cache_read": event.tokens.cache_read,
                "output": event.tokens.output,
                "cache_write": event.tokens.cache_write,
                "incomplete": event.tokens.input is None or event.tokens.output is None,
                "unpriced": False,
                "pico": "0",
                "price_revision_id": None,
                "price_version_id": None,
                "rule_applied": "none",
            },
            price_cache,
        )
        values.update(
            {
                name: 0
                for name in (
                    "eligible_input",
                    "eligible_cache",
                    "eligible_output",
                    "eligible_write",
                    "eligible_pico",
                )
            }
        )
        values["pending"] = 1
        accept(event.occurred_at, key, event.model_id or "模型未知", values)

    # Retired copies preserve new dimensions; they are subtracted from the old
    # minute/day authority before accepting its legacy, unknown-version remainder.
    dimension_clause = ""
    dimension_args: list[str] = []
    legacy_allowed = (
        not session_scoped
        and (not selected[2] or "" in selected[2])
        and (not selected[3] or "" in selected[3])
    )
    for index, column in enumerate(("harness", "vendor_id", "group_id", "version_id", "model_key")):
        choices = selected[index]
        if choices and (not legacy_allowed or index in (0, 1, 4)):
            dimension_clause += f" AND {column} IN ({','.join('?' for _ in choices)})"
            dimension_args.extend(sorted(choices))
    day_start = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = local_end.replace(hour=0, minute=0, second=0, microsecond=0)
    if local_end != day_end:
        day_end += timedelta(days=1)
    hbegin, hend = format_instant(Instant(day_start)), format_instant(Instant(day_end))
    recent_clause, recent_args = dimension_clause, list(dimension_args)
    for name, column in (("sessionRefs", "session_ref"), ("activityBatchIds", "activity_id")):
        if query.get(name):
            choices = query[name]
            recent_clause += " AND " + column + " IN (" + ",".join("?" for _ in choices) + ")"
            recent_args.extend(choices)
    for row in db.execute(
        "SELECT * FROM usage_recent WHERE bucket>=? AND bucket<?" + recent_clause,
        (begin if session_scoped else hbegin, finish if session_scoped else hend, *recent_args),
    ):
        check_budget()
        key = (row["harness"], row["vendor_id"], row["group_id"], row["version_id"], row["model_key"])
        values = decompress(row["payload"])
        if legacy_allowed:
            known.setdefault(history_key(row["bucket"], key), ExploreTotals()).add(values)
        if begin <= row["bucket"] < finish:
            accept(row["bucket"], key, row["model_id"], values, include_quality=session_scoped)
    for row in db.execute(
        "SELECT * FROM usage_dimensions WHERE end_at>? AND bucket<?"
        + (" AND 0" if session_scoped else "")
        + dimension_clause,
        (begin, finish, *dimension_args),
    ):
        key = (row["harness"], row["vendor_id"], row["group_id"], row["version_id"], row["model_key"])
        if not accepts(key, base=True):
            continue
        values = decompress(row["payload"])
        if legacy_allowed:
            known.setdefault(history_key(row["bucket"], key), ExploreTotals()).add(values)
        if "quality" in sections and accepts(key):
            quality.add(
                local(row["bucket"]).strftime("%Y-%m" if row["grain"] == "month" else "%Y-%m-%d"),
                key,
                row["model_id"],
                legacy_values(values),
            )
        check_budget()
        accept(row["bucket"], key, row["model_id"], values, True)
        if row["bucket"] < begin or row["end_at"] > finish:
            notices.add("已合并历史包含完整边界业务日或月份。")

    if "quality" in sections and not session_scoped:
        for row in db.execute(
            "SELECT * FROM usage_quality_legacy WHERE end_at>? AND bucket<?" + dimension_clause,
            (begin, finish, *dimension_args),
        ):
            check_budget()
            key = tuple(row[name] for name in ("harness", "vendor_id", "group_id", "version_id", "model_key"))
            if accepts(key):
                quality.add(
                    local(row["bucket"]).strftime("%Y-%m" if row["grain"] == "month" else "%Y-%m-%d"),
                    key,
                    row["model_id"],
                    decompress(row["payload"]),
                )
        for row in db.execute(
            "SELECT * FROM usage_quality_day WHERE end_at>? AND bucket<?" + dimension_clause,
            (begin, finish, *dimension_args),
        ):
            check_budget()
            key = tuple(row[name] for name in ("harness", "vendor_id", "group_id", "version_id", "model_key"))
            if accepts(key):
                quality.add(
                    local(row["bucket"]).strftime("%Y-%m-%d"),
                    key,
                    row["model_id"],
                    decompress(row["payload"]),
                )

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
    if legacy_allowed:
        # Full business boundary days are necessary to subtract dimensional days.
        day_start = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = local_end.replace(hour=0, minute=0, second=0, microsecond=0)
        if local_end != day_end:
            day_end += timedelta(days=1)
        hbegin, hend = format_instant(Instant(day_start)), format_instant(Instant(day_end))
        historic_clause = ""
        historic_args: list[str] = []
        for index, column in ((0, "harness"), (1, "vendor_id"), (4, "model_key")):
            if selected[index]:
                historic_clause += f" AND {column} IN ({','.join('?' for _ in selected[index])})"
                historic_args.extend(sorted(selected[index]))
        historic(
            dict(r)
            for r in db.execute(
                "SELECT * FROM usage_rollup WHERE bucket>=? AND bucket<?" + historic_clause,
                (hbegin, hend, *historic_args),
            )
        )
        for source in sorted(selected[0]) or ["all"]:
            for model in sorted(selected[4]) or [None]:
                historic(archived_rows(db, hbegin, hend, source, model_key=model))
                historic(
                    counter
                    for _, counter in daily_rows(
                        db, hbegin, hend, source, model, vendor_ids=tuple(sorted(selected[1]))
                    )
                )
        for identity, (stamp, name, counters) in legacy.items():
            if identity in known:
                # Historical quality booleans are OR flags, not subtractable counts.
                subtract = {
                    k: v
                    for k, v in known[identity].values.items()
                    if k not in ("incomplete", "unpriced") and not k.startswith(("eligible_", "quality_"))
                }
                counters.add(subtract, -1)
            if counters.values["records"] < 0:
                raise DomainError("ANALYSIS_INVALID", "历史维度与权威汇总不一致")
            if counters.values["records"]:
                accept(stamp, (identity[1], identity[2], "", "", identity[3]), name, counters.values, True)
                if "quality" in sections and accepts((identity[1], identity[2], "", "", identity[3])):
                    quality.add(
                        identity[0], (identity[1], identity[2], "", "", identity[3]), name, counters.values
                    )
                notices.add("旧版历史缺少价格组/倍率版本，未推测归属，每美元计算仅采用证据完整样本。")
        if legacy and (begin != hbegin or finish != hend):
            notices.add("已合并历史包含完整边界业务日，近期明细仍按所选时间。")
    elif any(selected[2:4]):
        notices.add("价格组/倍率筛选仅包含可确认历史归属的记录，缺失版本的旧记录不计入。")
    from htd.adapters.sqlite.calibration_costs import projection

    costs = (
        projection(db, dict(query, start=begin, end=finish))
        if sections != {"quality"}
        else CostProjection(query)
    )
    total_view = (
        costs.metrics(total.export())
        if "totals" in sections
        else CostProjection(query).metrics(total.export())
    )
    ranked = sorted(
        [
            {"modelKey": key, "displayName": name, **costs.scoped(modelKeys=[key]).metrics(value.export())}
            for key, (name, value) in models.items()
        ],
        key=lambda item: (-int(item["picoUsd"]), item["modelKey"]),
    )
    offset, limit = query.get("offset", 0), query.get("limit", 50)
    issues = quality.export(db) if "quality" in sections else []
    reasons = {REASONS[key] for key in query.get("qualityReasons", [])}
    if reasons:
        issues = [item for item in issues if reasons.intersection(item["reasons"])]
    unacknowledged = sum(not item["acknowledged"] for item in issues)
    status = query.get("qualityStatus", "all")
    if status != "all":
        issues = [item for item in issues if item["acknowledged"] == (status == "acknowledged")]
    snapshot = dict(
        db.execute("SELECT scope,value FROM revision WHERE scope IN ('usage','account','quality','pricing')")
    )
    snapshot["cost"] = costs.revision
    qoffset = query.get("qualityOffset", 0)

    def points(values: dict[str, ExploreTotals], selection: Any = costs) -> list[dict[str, Any]]:
        if not values and not trend:
            return []
        cursor = (
            start.value.replace(minute=0, second=0, microsecond=0)
            if grain == "hour"
            else local(begin).replace(hour=0, minute=0, second=0, microsecond=0)
        )
        if grain in ("month", "year"):
            cursor = cursor.replace(day=1, month=1 if grain == "year" else cursor.month)
        result: list[dict[str, Any]] = []
        while cursor < end.value:
            if len(result) >= 1500:
                raise DomainError("ANALYSIS_LIMIT", "趋势超过展示预算，请扩大时间粒度")
            key = (
                format_instant(Instant(cursor))
                if grain == "hour"
                else cursor.strftime("%Y" if grain == "year" else "%Y-%m" if grain == "month" else "%Y-%m-%d")
            )
            label = cursor.astimezone(zone).strftime("%m-%d %H:%M %z") if grain == "hour" else key
            result.append(
                {
                    "bucket": key,
                    "label": label,
                    **selection.period(key, grain, zone_name).metrics(
                        values.get(key, ExploreTotals()).export()
                    ),
                }
            )
            cursor = (
                cursor.replace(year=cursor.year + 1)
                if grain == "year"
                else (cursor + timedelta(days=32)).replace(day=1)
                if grain == "month"
                else cursor + timedelta(hours=1)
                if grain == "hour"
                else cursor + timedelta(days=1)
            )
        return result

    trend_view = points(trend) if "trend" in sections else []
    trend_gap = (
        int(total_view["correctionPico"]) - sum(int(row["correctionPico"]) for row in trend_view)
        if trend_view and "totals" in sections
        else 0
    )
    notice_codes = {
        "旧版历史缺少价格组/倍率版本，未推测归属，每美元计算仅采用证据完整样本。": "legacy_dimensions",
        "价格组/倍率筛选仅包含可确认历史归属的记录，缺失版本的旧记录不计入。": "version_coverage",
        "已合并历史包含完整边界业务日或月份。": "boundary_history",
        "已合并历史包含完整边界业务日，近期明细仍按所选时间。": "boundary_day",
        "较早历史按月合并，包含完整边界月份。": "boundary_month",
        "所选范围包含按月合并历史，统一按月展示。": "history_month",
        "所选范围包含日/月历史，统一按天展示，不伪造小时明细。": "history_day",
        "超过七天按天展示，小时明细可缩小范围后查看。": "grain_day",
        "较早记录只保留日/月汇总，未伪造小时明细。": "coarse_history",
        "退休分钟包含完整边界分钟，已返回实际范围。": "boundary_minute",
    }

    def comparison_view(identity: str, values: dict[str, ExploreTotals]) -> dict[str, object]:
        # Reuse the existing bounded comparison buckets, without another history read.
        combined = ExploreTotals()
        for metrics in values.values():
            combined.add(metrics.values)
        dimension = {
            "vendor": "vendorIds",
            "group": "groupIds",
            "version": "versionIds",
            "model": "modelKeys",
        }[query.get("compareDimension", "vendor")]
        projection = costs.scoped(**{dimension: [identity]})
        return {
            "id": identity,
            "totals": projection.metrics(combined.export()),
            "trend": points(values, projection),
        }

    return {
        "start": begin,
        "end": finish,
        "generatedAt": format_instant(at),
        "businessTimezone": zone_name,
        "grain": grain,
        "totals": total_view,
        "harnesses": [
            {"harness": key, "displayName": key, **costs.scoped(harnesses=[key]).metrics(value.export())}
            for key, value in sorted(harness_totals.items())
        ],
        "models": ranked[offset : offset + limit],
        "totalModels": len(models),
        "nextOffset": offset + limit if offset + limit < len(models) else None,
        "trend": trend_view,
        "unallocatedTrendCorrectionPico": str(trend_gap),
        "breakdowns": [
            {
                "harness": key[0],
                "vendorId": key[1],
                "groupId": key[2],
                "versionId": key[3],
                "modelKey": key[4],
                "displayName": name,
                **costs.scoped(
                    harnesses=[key[0]],
                    vendorIds=[key[1]],
                    groupIds=[key[2]],
                    versionIds=[key[3]],
                    modelKeys=[key[4]],
                ).metrics(value.export()),
            }
            for key, (name, value) in sorted(groups.items(), key=lambda item: -item[1][1].values["pico"])[
                :100
            ]
        ],
        "missingVersionRecords": str(unknown_records),
        "notices": sorted(notices),
        "coverage": [{"code": notice_codes[notice], "message": notice} for notice in sorted(notices)]
        + (
            [
                {
                    "code": "calibration_unallocated",
                    "message": "部分校准未明确归属到当前模型或范围，未分配金额单列，不按比例分摊。",
                }
            ]
            if total_view["allocationIncomplete"]
            else []
        ),
        "snapshot": snapshot,
        "costBasis": "local",
        "plan": QueryPlan.build(query, at).describe(),
        "comparisons": [comparison_view(identity, values) for identity, values in comparison.items()],
        "revision": db.execute("SELECT value FROM revision WHERE scope='usage'").fetchone()[0],
        "quality": issues if query.get("internalFull") else issues[qoffset : qoffset + 100],
        "qualityTotal": len(issues),
        "qualityUnacknowledged": unacknowledged,
        "qualityNextOffset": qoffset + 100 if qoffset + 100 < len(issues) else None,
    }
