"""Preserve legacy preference text and produce compatible active filters for the new IPC."""

from datetime import UTC, datetime, timedelta
from typing import Any

from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant
from htd.migration.legacy.merge import LegacyMerged


def _filter(data: dict[str, Any], timezone: str) -> dict[str, Any]:
    preset = data["preset"]
    if preset not in ("today", "7d", "30d", "custom"):
        raise DomainError("LEGACY_CONFIGURATION_INVALID", "旧筛选范围无效", "filters.preset")
    result: dict[str, Any] = {"range": {"7d": "7days", "30d": "30days"}.get(preset, preset)}
    if preset == "custom":
        try:
            start, end = (datetime.fromisoformat(data[name]) for name in ("start", "end"))
            if any(value.tzinfo is not None for value in (start, end)):
                raise ValueError("Legacy filters must contain local times")
            if timezone == "UTC":
                start, end = start.replace(tzinfo=UTC), end.replace(tzinfo=UTC)
            else:
                start, end = start.astimezone(UTC), end.astimezone(UTC)
            if not timedelta(0) < end - start <= timedelta(days=30):
                raise ValueError("Unsupported custom range")
            result.update(start=format_instant(Instant(start)), end=format_instant(Instant(end)))
        except (KeyError, ValueError):
            # Keep original text separately instead of blocking startup with an unusable active range.
            result = {"range": "today"}
    return result


def preferences(merged: LegacyMerged) -> dict[str, Any]:
    settings = merged.main["settings"]
    page = merged.main.get("activePage", "codex")
    if settings["retention"] not in ("90", "180", "360", "forever"):
        raise DomainError("LEGACY_CONFIGURATION_INVALID", "旧保留期限无效", "settings.retention")
    return {
        "theme": settings["theme"],
        "timezone": settings["timezone"],
        "showScanCountdown": settings["showScanCountdown"],
        "retention": "forever" if settings["retention"] == "forever" else int(settings["retention"]),
        "lastPage": {"prices": "pricing", "analysis": "overview"}.get(page, page),
        "collapsed": merged.main["collapsed"],
        "historyPrompted": merged.main["historyPrompted"],
        "legacyFilters": merged.main["filters"],
        "filters": {
            name: _filter(data, settings["timezone"]) for name, data in merged.main["filters"].items()
        },
    }


def preference_warnings(merged: LegacyMerged) -> tuple[str, ...]:
    active = preferences(merged)["filters"]
    return tuple(
        f"{name} 自定义筛选范围无法直接使用，原字符串已保留于 legacyFilters，新版使用今天"
        for name, data in merged.main["filters"].items()
        if data["preset"] == "custom" and active[name]["range"] != "custom"
    )
