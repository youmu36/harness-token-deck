"""Immutable in-memory catalog with historical resolution and stable fingerprints."""

import hashlib
import json
from dataclasses import asdict, dataclass

from htd.domain.common.errors import (
    GROUP_DUPLICATE,
    GROUP_LIMIT,
    GROUP_NOT_FOUND,
    PRICING_INVALID,
    VERSION_DUPLICATE,
    VERSION_NOT_FOUND,
    DomainError,
)
from htd.domain.common.time import Instant, format_instant
from htd.domain.pricing.model import (
    EditorHints,
    HarnessSelection,
    ModelRate,
    PriceGroup,
    PriceRevision,
    PriceVersion,
    SwitchPlan,
    VersionActivation,
)
from htd.domain.usage.model import Harness
from htd.domain.usage.model_key import model_key


@dataclass(frozen=True, slots=True)
class Resolution:
    group: PriceGroup | None = None
    version: PriceVersion | None = None
    revision: PriceRevision | None = None

    def rate_for(self, model: str | None) -> ModelRate | None:
        if model is None or self.revision is None:
            return None
        for rate in self.revision.models:
            if rate.model_id == model:
                return rate
        key = model_key(model)
        return next((rate for rate in self.revision.models if rate.key == key), None)


@dataclass(frozen=True, slots=True)
class Catalog:
    groups: tuple[PriceGroup, ...] = ()
    versions: tuple[PriceVersion, ...] = ()
    revisions: tuple[PriceRevision, ...] = ()
    activations: tuple[VersionActivation, ...] = ()
    selections: tuple[HarnessSelection, ...] = ()
    hints: tuple[EditorHints, ...] = ()
    switches: tuple[SwitchPlan, ...] = ()
    revision: int = 0
    sequence: int = 0

    def __post_init__(self) -> None:
        for name, kind in (
            ("groups", PriceGroup),
            ("versions", PriceVersion),
            ("revisions", PriceRevision),
            ("activations", VersionActivation),
            ("selections", HarnessSelection),
            ("hints", EditorHints),
            ("switches", SwitchPlan),
        ):
            values = getattr(self, name)
            if type(values) not in (tuple, list) or any(type(value) is not kind for value in values):
                raise DomainError(PRICING_INVALID, "价格目录记录类型无效", name)
            object.__setattr__(self, name, tuple(values))
        if any(type(value) is not int or value < 0 for value in (self.revision, self.sequence)):
            raise DomainError(PRICING_INVALID, "目录修订与历史序号须为非负整数", "revision")
        if sum(group.deleted_at is None for group in self.groups) > 100:
            raise DomainError(GROUP_LIMIT, "最多保留 100 个价格组", "groups")
        if len({group.name for group in self.groups}) != len(self.groups):
            raise DomainError(GROUP_DUPLICATE, "价格组名称重复", "name")
        for records in (self.groups, self.versions, self.revisions):
            if len({record.id for record in records}) != len(records):
                raise DomainError(PRICING_INVALID, "价格实体标识重复", "id")
        group_ids = {group.id for group in self.groups}
        versions = {version.id: version for version in self.versions}
        if any(version.group_id not in group_ids for version in self.versions):
            raise DomainError(PRICING_INVALID, "版本引用了不存在的价格组", "group_id")
        if len({(version.group_id, version.number) for version in self.versions}) != len(self.versions):
            raise DomainError(VERSION_DUPLICATE, "同组版本号重复", "number")
        if any(revision.version_id not in versions for revision in self.revisions):
            raise DomainError(PRICING_INVALID, "修订引用了不存在的版本", "version_id")
        if any(not any(v.group_id == group.id for v in self.versions) for group in self.groups):
            raise DomainError(PRICING_INVALID, "每个价格组至少需要一个版本", "versions")
        if any(not any(r.version_id == version.id for r in self.revisions) for version in self.versions):
            raise DomainError(PRICING_INVALID, "每个版本至少需要初始修订", "revisions")
        if any(
            activation.version_id not in versions
            or versions[activation.version_id].group_id != activation.group_id
            for activation in self.activations
        ):
            raise DomainError(PRICING_INVALID, "激活记录的版本不属于该组", "activations")
        if any(selection.group_id not in group_ids for selection in self.selections):
            raise DomainError(PRICING_INVALID, "选择记录引用了不存在的组", "selections")
        history = (*self.revisions, *self.activations, *self.selections)
        if len({record.seq for record in history}) != len(history):
            raise DomainError(PRICING_INVALID, "历史序号必须全局唯一", "seq")
        object.__setattr__(
            self, "sequence", max(self.sequence, max((record.seq for record in history), default=0))
        )
        if any(hint.version_id not in versions for hint in self.hints) or len(
            {(hint.version_id, model_key(hint.model_id)) for hint in self.hints}
        ) != len(self.hints):
            raise DomainError(PRICING_INVALID, "编辑提示的版本无效或模型重复", "hints")
        if len({plan.operation_id for plan in self.switches}) != len(self.switches):
            raise DomainError(PRICING_INVALID, "操作标识重复", "operation_id")

    def group(self, group_id: str) -> PriceGroup:
        for group in self.groups:
            if group.id == group_id:
                return group
        raise DomainError(GROUP_NOT_FOUND, "价格组不存在", "group_id")

    def version(self, version_id: str) -> PriceVersion:
        for version in self.versions:
            if version.id == version_id:
                return version
        raise DomainError(VERSION_NOT_FOUND, "价格版本不存在", "version_id")

    @property
    def latest_time(self) -> Instant | None:
        times = [record.effective_at for record in (*self.revisions, *self.activations, *self.selections)]
        times.extend(version.created_at for version in self.versions)
        times.extend(version.deleted_at for version in self.versions if version.deleted_at is not None)
        return max(times, default=None)

    def selection_at(self, harness: Harness, at: Instant) -> HarnessSelection | None:
        harness = Harness(harness)
        return max(
            (
                selection
                for selection in self.selections
                if selection.harness == harness and selection.effective_at <= at
            ),
            key=lambda selection: (selection.effective_at, selection.seq),
            default=None,
        )

    def active_version(self, group_id: str, at: Instant) -> PriceVersion | None:
        self.group(group_id)
        history = [activation for activation in self.activations if activation.group_id == group_id]
        if history:
            latest = max(
                (activation for activation in history if activation.effective_at <= at),
                key=lambda activation: (activation.effective_at, activation.seq),
                default=None,
            )
            if latest is not None:
                return self.version(latest.version_id)
            # Legacy groups may acquire their first explicit activation long after
            # creation. Requests before that activation still use creation history.
        # Stored creation order breaks equal-time ties; version numbers have no role here.
        candidates = [
            (index, version)
            for index, version in enumerate(self.versions)
            if version.group_id == group_id and version.created_at <= at
        ]
        latest_version = max(candidates, key=lambda item: (item[1].created_at, item[0]), default=None)
        return latest_version[1] if latest_version else None

    def revision_at(self, version_id: str, at: Instant) -> PriceRevision:
        self.version(version_id)
        history = sorted(
            (revision for revision in self.revisions if revision.version_id == version_id),
            key=lambda revision: (revision.effective_at, revision.seq),
        )
        eligible = [revision for revision in history if revision.effective_at <= at]
        return eligible[-1] if eligible else history[0]

    def resolve(self, harness: Harness, at: Instant) -> Resolution:
        selection = self.selection_at(harness, at)
        if selection is None:
            return Resolution()
        group = self.group(selection.group_id)
        if group.deleted_at is not None and at >= group.deleted_at:
            return Resolution()
        version = self.active_version(group.id, at)
        return Resolution(group, version, self.revision_at(version.id, at) if version else None)

    @property
    def fingerprint(self) -> str:
        payload = {
            "groups": sorted(
                (group.id, format_instant(group.deleted_at) if group.deleted_at else None)
                for group in self.groups
            ),
            "versions": [
                (version.id, version.group_id, format_instant(version.created_at))
                for version in self.versions
            ],
            "revisions": [
                (
                    revision.id,
                    revision.version_id,
                    format_instant(revision.effective_at),
                    revision.seq,
                    [asdict(model) for model in revision.models],
                )
                for revision in sorted(self.revisions, key=lambda record: record.seq)
            ],
            "activations": [
                (
                    activation.group_id,
                    activation.version_id,
                    format_instant(activation.effective_at),
                    activation.seq,
                )
                for activation in sorted(self.activations, key=lambda record: record.seq)
            ],
            "selections": [
                (
                    selection.harness.value,
                    selection.group_id,
                    format_instant(selection.effective_at),
                    selection.seq,
                )
                for selection in sorted(self.selections, key=lambda record: record.seq)
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
