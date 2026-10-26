"""Canonical scopes and explicit recent/calendar capability validation, without storage I/O."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant

SCOPE_LISTS = (
    "harnesses",
    "sessionRefs",
    "activityBatchIds",
    "vendorIds",
    "groupIds",
    "versionIds",
    "modelKeys",
    "sections",
    "compareIds",
    "qualityReasons",
)


@dataclass(frozen=True)
class QueryPlan:
    parameters: dict[str, Any]
    recent_boundary: str
    layer: str

    @classmethod
    def build(cls, query: dict[str, Any], at: Instant) -> "QueryPlan":
        parameters = dict(query)
        start, end = parse_instant(query["start"]), parse_instant(query["end"])
        span = end.value - start.value
        if span <= timedelta() or span > timedelta(days=36600):
            raise DomainError("RANGE_INVALID", "请选择有效且不超过一百年的范围")
        boundary = at.value - timedelta(days=62)
        kind = query.get("rangeKind")
        if kind in ("custom", "recent"):
            if span > timedelta(days=30):
                raise DomainError("RANGE_TOO_LONG", "近期和自定义范围最多 30 天")
            if start.value < boundary:
                raise DomainError(
                    "HISTORY_CAPABILITY", "自定义范围须在最近 62 天内，更早数据请选择历史季度或年度"
                )
            if end > at:
                raise DomainError("RANGE_INVALID", "近期范围不能包含未来时间")
        if (query.get("sessionRefs") or query.get("activityBatchIds")) and start.value < boundary:
            raise DomainError("HISTORY_CAPABILITY", "所选会话/活动详细能力仅保留最近 62 天")
        for name in SCOPE_LISTS:
            if name in parameters:
                parameters[name] = sorted(set(parameters[name]))
        parameters["start"], parameters["end"] = format_instant(start), format_instant(end)
        return cls(
            parameters,
            format_instant(Instant(boundary)),
            "recent" if start.value >= boundary else "day_month" if end.value <= boundary else "mixed",
        )

    def describe(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "recentBoundary": self.recent_boundary,
            "sessionScoped": bool(
                self.parameters.get("sessionRefs") or self.parameters.get("activityBatchIds")
            ),
            "rangeKind": self.parameters.get("rangeKind"),
        }
