"""Validated source-independent usage values and parser result types."""

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Literal

from htd.domain.common.errors import (
    CACHE_EXCEEDS_INPUT,
    INVALID_TOKEN_COUNT,
    REASONING_EXCEEDS_OUTPUT,
    UNKNOWN_HARNESS,
    USAGE_EVENT_INVALID,
    DomainError,
)
from htd.domain.common.time import format_instant, parse_instant
from htd.domain.usage.model_key import model_key

MAX_TOKEN_COUNT: Final = 2**53 - 1
OUTPUT_TIMING_UNCERTAIN: Final = "output_timing_uncertain"
type UsageStatus = Literal["reported", "completed", "error", "cancelled"]
type SourceKind = Literal["desktop", "desktop_subagent"]
_DEDUPE_KEY = re.compile(r"[0-9a-f]{64}")
_QUALITY_ISSUES = (
    "model_unknown",
    "usage_incomplete",
    "cache_unknown",
    "cache_write_unknown",
    "cache_write_pricing_unsupported",
)


class Harness(StrEnum):
    CODEX = "codex"
    ZCODE = "zcode"
    DSH = "dsh"
    CLAUDE = "claude"

    @classmethod
    def _missing_(cls, value: object) -> "Harness":
        raise DomainError(UNKNOWN_HARNESS, "未知的 Harness 来源", "harness")


def optional_text(value: object) -> str | None:
    """Extract display text only; identity hashes must use the original value."""
    if type(value) is not str:
        return None
    text = value.strip()
    try:
        return text if text and len(text.encode("utf-8")) <= 512 else None
    except UnicodeEncodeError:
        return None


@dataclass(frozen=True, slots=True)
class Tokens:
    input: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    output: int | None = None
    reasoning: int | None = None

    def __post_init__(self) -> None:
        for name in ("input", "cache_read", "cache_write", "output", "reasoning"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 0 <= value <= MAX_TOKEN_COUNT):
                raise DomainError(INVALID_TOKEN_COUNT, "Token 数须为安全范围内的非负整数", name)
        if self.input is not None:
            for name in ("cache_read", "cache_write"):
                value = getattr(self, name)
                if value is not None and value > self.input:
                    raise DomainError(CACHE_EXCEEDS_INPUT, "缓存 token 不能超过输入总量", name)
            if (
                self.cache_read is not None
                and self.cache_write is not None
                and self.cache_read + self.cache_write > self.input
            ):
                raise DomainError(CACHE_EXCEEDS_INPUT, "缓存读取与写入之和不能超过输入总量", "input")
        if self.reasoning is not None and self.output is not None and self.reasoning > self.output:
            raise DomainError(REASONING_EXCEEDS_OUTPUT, "推理 token 不能超过输出总量", "reasoning")


@dataclass(frozen=True, slots=True)
class UsageEvent:
    harness: Harness
    dedupe_key: str
    session_id: str | None
    request_id: str
    model_id: str | None
    occurred_at: str
    tokens: Tokens
    status: UsageStatus
    issues: tuple[str, ...] = ()
    source_kind: SourceKind | None = None
    provider_name: str | None = None
    generation_milliseconds: int | None = None
    time_precision: Literal["request_completion"] = field(default="request_completion", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "harness", Harness(self.harness))
        if type(self.dedupe_key) is not str or _DEDUPE_KEY.fullmatch(self.dedupe_key) is None:
            raise DomainError(USAGE_EVENT_INVALID, "去重键必须为 64 位小写十六进制", "dedupe_key")
        request_id = optional_text(self.request_id)
        session_id = optional_text(self.session_id)
        if request_id is None:
            raise DomainError(USAGE_EVENT_INVALID, "请求标识不能为空", "request_id")
        if session_id is None and self.harness in (Harness.CLAUDE, Harness.CODEX, Harness.DSH):
            raise DomainError(USAGE_EVENT_INVALID, "此来源必须提供会话标识", "session_id")
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "provider_name", optional_text(self.provider_name))
        object.__setattr__(self, "session_id", session_id)
        model_id = optional_text(self.model_id)
        if model_id is not None:
            model_key(model_id)
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "occurred_at", format_instant(parse_instant(self.occurred_at)))
        if type(self.tokens) is not Tokens:
            raise DomainError(USAGE_EVENT_INVALID, "用量必须为已校验的 Tokens", "tokens")
        if self.generation_milliseconds is not None and (
            type(self.generation_milliseconds) is not int
            or not 0 < self.generation_milliseconds <= MAX_TOKEN_COUNT
        ):
            raise DomainError(USAGE_EVENT_INVALID, "生成耗时须为正整数毫秒", "generation_milliseconds")
        statuses = ("completed", "error", "cancelled") if self.harness == Harness.ZCODE else ("reported",)
        if type(self.status) is not str or self.status not in statuses:
            raise DomainError(USAGE_EVENT_INVALID, "状态与 Harness 来源不匹配", "status")
        if self.source_kind is not None and (
            self.harness != Harness.CODEX
            or type(self.source_kind) is not str
            or self.source_kind not in ("desktop", "desktop_subagent")
        ):
            raise DomainError(USAGE_EVENT_INVALID, "来源分类与 Harness 不匹配", "source_kind")
        if type(self.issues) not in (tuple, list) or any(
            type(issue) is not str or not issue.strip() for issue in self.issues
        ):
            raise DomainError(USAGE_EVENT_INVALID, "质量标记必须为非空字符串序列", "issues")
        # Recompute derived flags, including when dataclasses.replace changes the data.
        prefix = tuple(issue for issue in self.issues if issue not in _QUALITY_ISSUES)
        conditions = (
            model_id is None,
            self.tokens.input is None or self.tokens.output is None,
            self.tokens.cache_read is None,
            self.tokens.cache_write is None,
            False,  # Strip the obsolete unsupported-creation flag from older events.
        )
        generated = tuple(
            issue for issue, applies in zip(_QUALITY_ISSUES, conditions, strict=True) if applies
        )
        object.__setattr__(self, "issues", prefix + generated)


@dataclass(frozen=True, slots=True)
class Skip:
    """A source record unrelated to billable usage."""


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str

    def __post_init__(self) -> None:
        if type(self.code) is not str or not self.code.strip():
            raise DomainError(USAGE_EVENT_INVALID, "诊断码不能为空", "code")


type Result = UsageEvent | Skip | Diagnostic
