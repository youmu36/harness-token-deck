"""Render migration checks, wallet balances, and actionable failures as Markdown."""

from pathlib import Path

from htd.adapters.sqlite.usage_codec import dumps
from htd.domain.common.errors import DomainError
from htd.migration.verify import Verification


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def write_report(result: Verification, path: Path, *, elapsed_seconds: float = 0) -> Path:
    lines = [
        "# 旧数据迁移报告",
        "",
        f"结果: {'通过' if result.ok else '失败，禁止激活'}",
        "",
        f"来源版本: {result.source_version}",
        f"快照: `{result.source}`",
        f"SHA-256: `{result.sha256}`",
        f"新库: `{result.target}`",
        f"耗时: {elapsed_seconds:.3f} 秒",
        "",
        f"账本总额 (pico): 旧 {result.old_total} / 新 {result.new_total}",
        "",
        "## 行数",
        "",
        "| 表 | 旧 (可解析部分) | 新 |",
        "| --- | ---: | ---: |",
    ]
    lines.extend(f"| {_cell(name)} | {old} | {new} |" for name, (old, new) in result.counts.items())
    lines.extend(["", "## 校验", "", "| 项目 | 结果 |", "| --- | --- |"])
    lines.extend(
        f"| {_cell(name)} | {'通过' if passed else '失败'} |" for name, passed in result.checks.items()
    )
    lines.extend(
        [
            "",
            "## 钱包",
            "",
            "金额单位: pico。关联时间线按迁移时关联推定。",
            "",
            "| 供应商 | B_old | B_new | R | C | A | 覆盖起点 | 历史 | 关联组 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
        ]
    )
    lines.extend(
        f"| {_cell(w.name)} ({_cell(w.vendor_id)}) | {w.old} | {w.new} | {w.r} | {w.c} | {w.a} | "
        f"{w.coverage_start} | {'不完整' if w.incomplete_history else '尚存账本'} | "
        f"{_cell(', '.join(w.group_ids))} |"
        for w in result.wallets
    )
    lines.extend(
        ["", "## Harness x UTC 日期", "", "| 来源 / 日期 | 旧 pico | 新 pico |", "| --- | ---: | ---: |"]
    )
    lines.extend(f"| {_cell(key)} | {old} | {new} |" for key, (old, new) in result.daily.items())
    lines.extend(["", "## 价格原串与规范形式对照", ""])
    for mapping in result.price_mapping:
        lines.extend(
            [
                f"### {_cell(mapping['versionId'])} / {_cell(mapping['revisionId'])}",
                "",
                "```json",
                dumps(mapping),
                "```",
                "",
            ]
        )
    for heading, records in (
        ("错误", result.errors),
        ("警告", result.warnings),
        ("价格解析不一致", result.price_mismatches),
        ("丢弃的检查点", result.dropped_checkpoints),
    ):
        lines.extend(["", "## " + heading, ""])
        lines.extend(["- " + _cell(record) for record in records] or ["无"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_failure(
    path: Path, source: Path, target: Path, error: Exception, *, elapsed_seconds: float
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    detail = (
        f"{error.code} ({error.field}): {error.message}" if isinstance(error, DomainError) else str(error)
    )
    path.write_text(
        f"# 旧数据迁移报告\n\n结果: 失败，禁止激活\n\n快照: `{source}`\n新库: `{target}`\n"
        f"耗时: {elapsed_seconds:.3f} 秒\n\n错误: {detail}\n",
        encoding="utf-8",
    )
