"""Validated wallet entities and immutable plans supplied with caller-owned metadata."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from htu.domain.common.errors import AMOUNT_INVALID, PICO_INVALID, WALLET_INVALID, DomainError
from htu.domain.common.site_url import site_url
from htu.domain.common.time import Instant
from htu.domain.usage.model import Harness

type VendorStatus = Literal["active", "archived"]
type EntryKind = Literal["top_up", "consumption", "consumption_delta", "adjustment"]
_KEY = re.compile(r"[0-9a-f]{64}")


def validate_identifier(value: str, field: str) -> None:
    if type(value) is not str or not value.strip():
        raise DomainError(WALLET_INVALID, "标识不能为空", field)
    _text(value, field)


def _text(value: str, field: str) -> None:
    if type(value) is not str:
        raise DomainError(WALLET_INVALID, "文本须为字符串", field)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DomainError(WALLET_INVALID, "文本包含无效 Unicode 字符", field) from error


def vendor_name(name: str) -> str:
    _text(name, "name")
    result = name.strip()
    if not 1 <= len(result) <= 60:
        raise DomainError(WALLET_INVALID, "供应商名称须为 1 至 60 字符", "name")
    return result


def validate_instant(value: Instant, field: str) -> None:
    if type(value) is not Instant:
        raise DomainError(WALLET_INVALID, "时间须为规范化的 Instant", field)


def validate_sequence(value: int, field: str = "seq", *, minimum: int = 1) -> None:
    if type(value) is not int or value < minimum:
        raise DomainError(WALLET_INVALID, "序号或计数须为有效整数", field)


def validate_pico(value: int, field: str) -> None:
    if type(value) is not int:
        raise DomainError(PICO_INVALID, "金额须为整数 pico", field)


@dataclass(frozen=True, slots=True)
class Vendor:
    id: str
    name: str
    status: VendorStatus
    created_at: Instant
    url: str | None = None
    past_top_up_pico: int = 0
    past_consumption_pico: int = 0

    def __post_init__(self) -> None:
        validate_identifier(self.id, "id")
        object.__setattr__(self, "name", vendor_name(self.name))
        if type(self.status) is not str or self.status not in ("active", "archived"):
            raise DomainError(WALLET_INVALID, "供应商状态无效", "status")
        validate_instant(self.created_at, "created_at")
        object.__setattr__(self, "url", site_url(self.url))
        validate_pico(self.past_top_up_pico, "past_top_up_pico")
        if self.past_top_up_pico < 0:
            raise DomainError(AMOUNT_INVALID, "过往充值不能为负数", "past_top_up_pico")
        validate_pico(self.past_consumption_pico, "past_consumption_pico")
        if self.past_consumption_pico < 0:
            raise DomainError(AMOUNT_INVALID, "过往消费不能为负数", "past_consumption_pico")


@dataclass(frozen=True, slots=True)
class VendorLink:
    group_id: str
    vendor_id: str | None
    effective_at: Instant
    seq: int

    def __post_init__(self) -> None:
        validate_identifier(self.group_id, "group_id")
        if self.vendor_id is not None:
            validate_identifier(self.vendor_id, "vendor_id")
        validate_instant(self.effective_at, "effective_at")
        validate_sequence(self.seq)


@dataclass(frozen=True, slots=True)
class UsageRef:
    harness: Harness
    dedupe_key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "harness", Harness(self.harness))
        if type(self.dedupe_key) is not str or _KEY.fullmatch(self.dedupe_key) is None:
            raise DomainError(WALLET_INVALID, "请求身份须为 64 位小写十六进制", "dedupe_key")


@dataclass(frozen=True, slots=True)
class WalletEntry:
    id: str
    vendor_id: str
    kind: EntryKind
    amount_pico: int
    occurred_at: Instant
    seq: int
    operation_id: str | None = None
    usage_ref: UsageRef | None = None
    related_entry_id: str | None = None
    recalc_batch_id: str | None = None
    note: str = ""
    unpriced: bool = False
    incomplete: bool = False

    def __post_init__(self) -> None:
        for field in ("id", "vendor_id"):
            validate_identifier(getattr(self, field), field)
        for field in ("operation_id", "related_entry_id", "recalc_batch_id"):
            value = getattr(self, field)
            if value is not None:
                validate_identifier(value, field)
        if type(self.kind) is not str or self.kind not in (
            "top_up",
            "consumption",
            "consumption_delta",
            "adjustment",
        ):
            raise DomainError(WALLET_INVALID, "钱包凭证类型无效", "kind")
        validate_pico(self.amount_pico, "amount_pico")
        if (self.kind == "top_up" and self.amount_pico <= 0) or (
            self.kind == "consumption" and self.amount_pico < 0
        ):
            raise DomainError(AMOUNT_INVALID, "充值须大于零，消费金额不能为负", "amount_pico")
        validate_instant(self.occurred_at, "occurred_at")
        validate_sequence(self.seq)
        _text(self.note, "note")
        if self.usage_ref is not None and type(self.usage_ref) is not UsageRef:
            raise DomainError(WALLET_INVALID, "请求引用无效", "usage_ref")
        if type(self.unpriced) is not bool or type(self.incomplete) is not bool:
            raise DomainError(WALLET_INVALID, "质量标记须为布尔值", "unpriced")
        if self.related_entry_id == self.id:
            raise DomainError(WALLET_INVALID, "凭证不能关联自身", "related_entry_id")
        if self.kind in ("consumption", "consumption_delta"):
            if self.usage_ref is None:
                raise DomainError(WALLET_INVALID, "消费凭证须引用请求", "usage_ref")
            if self.kind == "consumption_delta" and (
                self.related_entry_id is None or self.recalc_batch_id is None
            ):
                raise DomainError(WALLET_INVALID, "重算差额须关联原消费和批次", "related_entry_id")
        elif self.usage_ref is not None or self.unpriced or self.incomplete:
            raise DomainError(WALLET_INVALID, "资金凭证不能携带请求质量标记", "usage_ref")
        if self.kind == "top_up" and self.operation_id is None:
            raise DomainError(WALLET_INVALID, "充值须有操作标识", "operation_id")
        if self.kind == "adjustment":
            if self.note not in ("calibration", "late_offset", "migration"):
                raise DomainError(WALLET_INVALID, "调整凭证须注明来源", "note")
            if self.note == "calibration" and self.operation_id is None:
                raise DomainError(WALLET_INVALID, "校准须有操作标识", "operation_id")
            if self.note == "late_offset" and self.related_entry_id is None:
                raise DomainError(WALLET_INVALID, "迟到抵消须关联消费凭证", "related_entry_id")


@dataclass(frozen=True, slots=True)
class Calibration:
    entry_id: str
    vendor_id: str
    at: Instant
    seq: int
    target_pico: int
    observed_balance_pico: int
    watermark_seq: int

    def __post_init__(self) -> None:
        validate_identifier(self.entry_id, "entry_id")
        validate_identifier(self.vendor_id, "vendor_id")
        validate_instant(self.at, "at")
        validate_sequence(self.seq)
        validate_sequence(self.watermark_seq, "watermark_seq", minimum=0)
        validate_pico(self.target_pico, "target_pico")
        validate_pico(self.observed_balance_pico, "observed_balance_pico")
        if self.watermark_seq >= self.seq:
            raise DomainError(WALLET_INVALID, "校准水位须早于校准凭证序号", "watermark_seq")

    @property
    def amount_pico(self) -> int:
        return self.target_pico - self.observed_balance_pico


@dataclass(frozen=True, slots=True)
class Totals:
    r: int = 0
    c: int = 0
    a: int = 0
    last_calibrated_at: Instant | None = None
    revision: int = 0
    watermark_seq: int = 0
    unpriced_count: int = 0
    incomplete_count: int = 0

    def __post_init__(self) -> None:
        for field in ("r", "c", "a"):
            validate_pico(getattr(self, field), field)
        if self.r < 0:
            raise DomainError(PICO_INVALID, "累计充值不能为负", "r")
        for field in ("revision", "watermark_seq", "unpriced_count", "incomplete_count"):
            validate_sequence(getattr(self, field), field, minimum=0)
        if self.last_calibrated_at is not None:
            validate_instant(self.last_calibrated_at, "last_calibrated_at")

    @property
    def b(self) -> int:
        return self.r - self.c + self.a

    @property
    def coverage_incomplete(self) -> bool:
        return self.unpriced_count > 0 or self.incomplete_count > 0


@dataclass(frozen=True, slots=True)
class CalibrationPlan:
    target_pico: int
    observed_balance_pico: int
    watermark_seq: int
    expected_revision: int

    def __post_init__(self) -> None:
        validate_pico(self.target_pico, "target_pico")
        validate_pico(self.observed_balance_pico, "observed_balance_pico")
        validate_sequence(self.watermark_seq, "watermark_seq", minimum=0)
        validate_sequence(self.expected_revision, "expected_revision", minimum=0)

    @property
    def amount_pico(self) -> int:
        return self.target_pico - self.observed_balance_pico

    def records(
        self, *, entry_id: str, vendor_id: str, at: Instant, seq: int, operation_id: str
    ) -> tuple[WalletEntry, Calibration]:
        audit = Calibration(
            entry_id, vendor_id, at, seq, self.target_pico, self.observed_balance_pico, self.watermark_seq
        )
        entry = WalletEntry(
            entry_id, vendor_id, "adjustment", self.amount_pico, at, seq, operation_id, note="calibration"
        )
        return entry, audit


@dataclass(frozen=True, slots=True)
class LateOffsetPlan:
    related_entry_id: str
    vendor_id: str
    amount_pico: int

    def __post_init__(self) -> None:
        validate_identifier(self.related_entry_id, "related_entry_id")
        validate_identifier(self.vendor_id, "vendor_id")
        validate_pico(self.amount_pico, "amount_pico")

    def record(self, *, entry_id: str, at: Instant, seq: int) -> WalletEntry:
        return WalletEntry(
            entry_id,
            self.vendor_id,
            "adjustment",
            self.amount_pico,
            at,
            seq,
            related_entry_id=self.related_entry_id,
            note="late_offset",
        )


@dataclass(frozen=True, slots=True)
class VendorDeletionPlan:
    action: Literal["delete", "archive"]
    vendor: Vendor | None
    unlinks: tuple[VendorLink, ...]


def entry_history(entries: Iterable[WalletEntry]) -> tuple[WalletEntry, ...]:
    records = tuple(entries)
    if any(type(record) is not WalletEntry for record in records):
        raise DomainError(WALLET_INVALID, "钱包历史须为已校验的凭证", "entries")
    if len({record.id for record in records}) != len(records) or len(
        {record.seq for record in records}
    ) != len(records):
        raise DomainError(WALLET_INVALID, "凭证标识或序号重复", "entries")
    consumed = [record.usage_ref for record in records if record.kind == "consumption"]
    if len(set(consumed)) != len(consumed):
        raise DomainError(WALLET_INVALID, "同一请求只能生成一条原消费凭证", "usage_ref")
    operations = [
        record.operation_id
        for record in records
        if record.kind == "top_up" or (record.kind == "adjustment" and record.note == "calibration")
    ]
    if len(set(operations)) != len(operations):
        raise DomainError(WALLET_INVALID, "用户操作已生成重复凭证", "operation_id")
    return records


def link_history(links: Iterable[VendorLink]) -> tuple[VendorLink, ...]:
    records = tuple(links)
    if any(type(record) is not VendorLink for record in records) or len(
        {record.seq for record in records}
    ) != len(records):
        raise DomainError(WALLET_INVALID, "关联历史类型无效或序号重复", "links")
    return records
