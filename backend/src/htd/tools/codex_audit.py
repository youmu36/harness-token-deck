"""Read-only Codex audit: source coverage, current-price estimates and ledger comparison."""

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from htd.adapters.collectors.codex_audit_reader import read_audit_file
from htd.adapters.collectors.codex_logs import CodexLogs
from htd.adapters.collectors.discovery import Discovery, source_key
from htd.adapters.sqlite.codex_audit import AuditLedger, read_audit_ledger
from htd.domain.billing.estimate import estimate_cost
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.pricing.catalog import Resolution
from htd.domain.usage.model import Harness, UsageEvent

_TOKEN_FIELDS = ("input", "cache_read", "cache_write", "output", "reasoning")


@dataclass(slots=True)
class AuditGroup:
    count: int = 0
    tokens: Counter[str] = field(default_factory=Counter[str])
    unknown: Counter[str] = field(default_factory=Counter[str])
    pico: int = 0
    unpriced: int = 0
    incomplete: int = 0

    def add(self, event: UsageEvent | None, pricing: Resolution) -> None:
        self.count += 1
        for name in _TOKEN_FIELDS:
            value = getattr(event.tokens, name) if event else None
            if value is None:
                self.unknown[name] += 1
            else:
                self.tokens[name] += value
        if event is None:
            self.unpriced += 1
            self.incomplete += 1
            return
        cost = estimate_cost(event.tokens, pricing.rate_for(event.model_id), parse_instant(event.occurred_at))
        self.pico += cost.pico_usd
        self.unpriced += int(cost.unpriced)
        self.incomplete += int(cost.incomplete)


@dataclass(slots=True)
class AuditReport:
    start: Instant
    end: Instant
    priced_at: Instant
    groups: dict[tuple[str, str, str], AuditGroup] = field(
        default_factory=dict[tuple[str, str, str], AuditGroup]
    )
    events: dict[str, UsageEvent] = field(default_factory=dict[str, UsageEvent])
    files: int = 0
    duplicates: int = 0
    conflicts: int = 0
    conflict_fields: Counter[str] = field(default_factory=Counter[str])
    conflict_token_delta: Counter[str] = field(default_factory=Counter[str])
    conflict_groups: Counter[str] = field(default_factory=Counter[str])
    conflict_locations: Counter[str] = field(default_factory=Counter[str])
    snapshots: int = 0
    interrupted: int = 0
    diagnostics: Counter[str] = field(default_factory=Counter[str])
    ledger: AuditLedger | None = None
    unmetered: tuple[dict[str, object], ...] | None = None
    source_available: bool = True


def audit(
    source: Path, start: Instant, end: Instant, *, db: Path | None = None, now: Instant | None = None
) -> AuditReport:
    if start >= end:
        raise ValueError("Require start < end")
    at = now or Instant(datetime.now(UTC))
    report = AuditReport(start, end, at)
    if db is not None:
        report.ledger = read_audit_ledger(db, start, end)
    pricing = report.ledger.catalog.resolve(Harness.CODEX, at) if report.ledger else Resolution()
    found = Discovery().discover(Harness.CODEX, source_key(source))
    report.source_available = found.available
    report.diagnostics.update(found.diagnostics)
    excluded: set[tuple[str, str]] = set()
    event_paths: dict[str, str] = {}
    for path in found.paths:
        # Commit per-file audit results only after the reader verifies its length/anchor snapshot.
        try:
            items = tuple(read_audit_file(path, start, end))
        except (OSError, ValueError) as error:
            code = str(error) if type(error) is ValueError else "source_read_failed"
            report.diagnostics[code] += 1
            continue
        report.files += 1
        for item in items:
            if item.reason == "snapshot":
                report.snapshots += 1
                continue
            if item.reason == "interrupted_turn":
                report.interrupted += 1
                continue
            if item.reason in (
                "invalid_json",
                "desktop_source_unverified",
                "request_usage_format_unverified",
            ):
                report.diagnostics[item.reason] += 1
                continue
            event = item.event
            if event is not None:
                if item.reason == "accepted":
                    previous = report.events.get(event.dedupe_key)
                    if previous is not None:
                        report.duplicates += 1
                        report.conflicts += int(previous != event)
                        for name in ("tokens", "model_id", "occurred_at", "source_kind", "provider_name"):
                            if getattr(previous, name) != getattr(event, name):
                                report.conflict_fields[name] += 1
                        if previous != event:
                            report.conflict_locations[
                                "same_file" if event_paths[event.dedupe_key] == path else "different_files"
                            ] += 1
                            report.conflict_groups[
                                event.occurred_at[:10] + " / " + (event.model_id or "(unknown)")
                            ] += 1
                            for name in _TOKEN_FIELDS:
                                old, new = getattr(previous.tokens, name), getattr(event.tokens, name)
                                if old is not None and new is not None:
                                    report.conflict_token_delta[name] += new - old
                        continue
                    report.events[event.dedupe_key] = event
                    event_paths[event.dedupe_key] = path
                else:
                    key = (item.reason, event.dedupe_key)
                    if key in excluded:
                        report.duplicates += 1
                        continue
                    excluded.add(key)
            key = (item.kind, item.model or "(unknown)", item.reason)
            group = report.groups.setdefault(key, AuditGroup())
            group.add(event, pricing)
    title_requests = CodexLogs().read(str(source), start, end)
    report.unmetered = None if title_requests is None else tuple(item.to_dict() for item in title_requests)
    return report


def _usd(pico: int) -> str:
    whole, part = divmod(pico, 10**12)
    return f"{whole}.{part:012d}"


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ").replace("`", "'")


def _tokens(group: AuditGroup, name: str) -> str:
    count = group.unknown[name]
    return str(group.tokens[name]) + (f" + ?({count})" if count else "")


def markdown(report: AuditReport) -> str:
    lines = [
        "# Codex 只读审计",
        "",
        f"区间: [{format_instant(report.start)}, {format_instant(report.end)}); 按 UTC 分日。",
        f"有效文件 {report.files}; 来源可计费请求 {len(report.events)}; 重复请求 {report.duplicates}; "
        f"重复内容冲突 {report.conflicts}; 累计快照 {report.snapshots}(不计费)。",
        "",
        f"当前价格快照: {format_instant(report.priced_at)}; 峰谷时段按各请求完成时间计算。",
        "估算金额仅为有价格部分的小计; 缺价和缺失 token 单独列出，未知量不代表零。",
        "被拒绝请求的估算仅用于调查，不入账。",
        "timestamp_unscoped 表示完成时间缺失或无效，无法归属区间，仅作诊断。",
        "",
        "| sourceKind | 模型 | 接受 / 拒绝原因 | 请求数 | input | cached | cache_write | output | "
        "reasoning | 估算 USD | 未定价 | 不完整 |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    if report.conflict_fields:
        lines.insert(
            4,
            "重复内容差异字段: "
            + ", ".join(f"{name}={count}" for name, count in sorted(report.conflict_fields.items()))
            + "。",
        )
        lines.insert(
            5,
            "重复记录 Token 差额(后见减首见，仅诊断，不追加计费): "
            + ", ".join(f"{name}={count}" for name, count in sorted(report.conflict_token_delta.items()))
            + "。",
        )
        lines.insert(
            6,
            "重复差异分组: "
            + ", ".join(f"{name}: {count}" for name, count in sorted(report.conflict_groups.items()))
            + "。",
        )
        lines.insert(
            7,
            "重复差异位置: "
            + ", ".join(f"{name}: {count}" for name, count in sorted(report.conflict_locations.items()))
            + "。",
        )
    for (kind, model, reason), group in sorted(report.groups.items()):
        amount = _usd(group.pico) if group.unpriced < group.count else "不可用"
        cells = [
            kind,
            _cell(model),
            reason,
            str(group.count),
            *(_tokens(group, name) for name in _TOKEN_FIELDS),
            amount,
            str(group.unpriced),
            str(group.incomplete),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.extend(["", "## 账本对比", ""])
    ledger = report.ledger
    if ledger is None:
        lines.append("账本与价格不可用: 未发现新版账本或未指定 --db; 未验证来源与账本相等。")
    else:
        source_keys, ledger_keys = set(report.events), set(ledger.rows)
        shared = source_keys & ledger_keys
        # Timing and source annotations are not immutable billing facts. Their
        # presence in a richer parser must not make matching token records differ.
        mismatched = sum(
            replace(report.events[key], provider_name=None, generation_milliseconds=None, issues=())
            != replace(ledger.rows[key][0], issues=())
            for key in shared
        )
        missing = source_keys - ledger_keys
        before_start = sum(
            ledger.started_at is not None
            and parse_instant(report.events[key].occurred_at) < ledger.started_at
            for key in missing
        )
        comparison = (
            f"来源 {len(source_keys)} / 账本 {len(ledger_keys)}; 匹配身份 {len(shared)}; "
            f"来源未入账 {len(missing)}(其中首次采集起点前 {before_start}); "
            f"账本来源缺失 {len(ledger_keys - source_keys)}; 字段不一致 {mismatched}。"
        )
        if ledger.compacted_records:
            lines.append(
                f"区间含 {ledger.compacted_records} 条已汇总请求和 {len(ledger_keys)} 条明细。"
                "历史仅作数值核对，不能将缺少请求明细判为漏采。"
            )
        else:
            lines.append(comparison)
            if ledger.last_scan_at is not None:
                after_scan = sum(
                    parse_instant(report.events[key].occurred_at) >= ledger.last_scan_at for key in missing
                )
                lines.append(
                    f"最近来源扫描: {format_instant(ledger.last_scan_at)}; "
                    f"未入账中时间早于最近扫描 {len(missing) - after_scan}，不早于最近扫描 {after_scan}。"
                )
        lines.append(
            f"账本实际金额: {_usd(sum(row[1] for row in ledger.rows.values()) + ledger.compacted_pico)} USD。"
        )
        source_group, ledger_group = AuditGroup(), AuditGroup()
        for event in report.events.values():
            source_group.add(event, Resolution())
        for event, _ in ledger.rows.values():
            ledger_group.add(event, Resolution())
        ledger_group.tokens.update(ledger.compacted_tokens)
        lines.extend(["", "| token | 来源 | 账本 |", "| --- | ---: | ---: |"])
        lines.extend(
            f"| {name} | {_tokens(source_group, name)} | {_tokens(ledger_group, name)} |"
            for name in _TOKEN_FIELDS
        )
        if ledger.compacted_records:
            lines.append("\n历史汇总已计入数值；逐请求字段一致性仅适用于保留的明细，不宣称全区间逐请求一致。")  # noqa: RUF001
        elif (
            source_keys == ledger_keys and not mismatched and not report.conflicts and not report.diagnostics
        ):
            lines.append("\n来源可计费请求 = 账本请求，token、模型、时间与来源分类一致。")
        else:
            lines.append("\n尚不能确认完整一致; 差异可能包含首次起点、历史未导入或来源读取不完整。")
    lines.extend(["", "## 无本地用量的请求", ""])
    if report.unmetered is None:
        lines.append("标题生成日志不可用。")
    else:
        lines.extend(["| UTC 日期 | 类别 | 成功次数 |", "| --- | --- | ---: |"])
        lines.extend(f"| {row['date']} | {row['kind']} | {row['count']} |" for row in report.unmetered)
        lines.append(f"\n标题生成成功请求合计: {sum(int(str(row['count'])) for row in report.unmetered)}。")
    lines.append(f"中断回合: {report.interrupted}; 中断回合数不能等同于已计费请求数。")
    lines.append("覆盖不足: 标题生成、中断及断流重试没有可验证的本地用量，不估算金额、不计入账本。")
    if report.diagnostics:
        lines.extend(["", "## 读取与格式诊断", ""])
        lines.extend(f"- {code}: {count}" for code, count in sorted(report.diagnostics.items()))
    return "\n".join(lines) + "\n"


def default_db() -> Path | None:
    from htd.adapters.data_directory import resolve_data_directory

    directory = resolve_data_directory().root
    for name in ("htd.sqlite3", "ledger.sqlite3"):
        candidate = Path(directory) / name
        if candidate.is_file():
            return candidate
    return None


def _protect_output(output: Path, source: Path, db: Path | None) -> None:
    resolved = output.resolve()
    if resolved.suffix.lower() != ".md" or (db is not None and resolved == db.resolve()):
        raise ValueError("Output must be a Markdown file separate from the database")
    if not output.exists():
        return
    protected = list(Discovery().discover(Harness.CODEX, source_key(source)).paths)
    log_home = source.parent if source.name.lower() in ("sessions", "archived_sessions") else source
    protected.append(str(log_home / "logs_2.sqlite"))
    if db is not None:
        protected.append(str(db))
    if any(Path(path).is_file() and output.samefile(path) for path in protected):
        raise ValueError("Output aliases a read-only audit input")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="含时区的 ISO 时间(含)")
    parser.add_argument("--end", required=True, help="含时区的 ISO 时间(不含)")
    parser.add_argument("--source", type=Path, help="sessions 目录或 Codex home(含归档)")
    parser.add_argument("--db", type=Path, help="新版账本，只读且不执行迁移")
    parser.add_argument("--out", type=Path, help="Markdown 报告; 省略则输出 stdout")
    args = parser.parse_args(argv)
    try:
        start, end = parse_instant(str(args.start)), parse_instant(str(args.end))
        source = args.source or Path(Discovery().default_path(Harness.CODEX))
        if (source / "sessions").is_dir():
            source = source / "sessions"
        db = args.db or default_db()
        if args.out is not None:
            _protect_output(args.out, source, db)
        output = markdown(audit(source, start, end, db=db))
        if args.out is None:
            sys.stdout.write(output)
        else:
            args.out.write_text(output, encoding="utf-8")
        return 0
    except Exception:
        sys.stderr.write("审计失败: 请检查时间区间、来源、账本格式与输出路径; 未修改来源或账本。\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
