"""Merge legacy main and extension configuration using the documented loading rules."""

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.usage.model_key import model_key
from htd.migration.legacy.reader import LegacySnapshot


@dataclass(frozen=True, slots=True)
class LegacyMerged:
    version: str
    main: dict[str, Any]
    extension: dict[str, Any]
    selections: tuple[dict[str, Any], ...]
    vendors: tuple[dict[str, Any], ...]
    group_vendors: dict[str, str]
    multipliers: dict[tuple[str, str], str]


def timestamp(value: str) -> str:
    return format_instant(parse_instant(value))


def merge_legacy(snapshot: LegacySnapshot, *, at: Instant | None = None) -> LegacyMerged:
    main = deepcopy(snapshot.state("configuration"))
    extension = deepcopy(snapshot.state("configuration_extensions"))
    at = at or parse_instant(snapshot.rows("configuration")[0]["updated_at"])
    try:
        if main.get("schemaVersion") != 1 or type(main.get("groups")) is not list:
            raise ValueError("Unsupported configuration schema")
        groups: list[dict[str, Any]] = main["groups"]
        group_ids = {group["id"] for group in groups}
        selections: list[dict[str, Any]] = deepcopy(main.get("selections", []))
        selections.extend(
            deepcopy(value) for value in extension.get("dshSelections", []) if value["groupId"] in group_ids
        )
        if snapshot.identity.user_version == 7:
            selections.extend(
                {"harness": "dsh", "groupId": row["group_id"], "effectiveAt": row["effective_at"]}
                for row in snapshot.rows("price_selections")
                if row["harness"] == "dsh"
            )
        last: dict[str, str] = {}
        for index, selection in enumerate(selections):
            stamp = timestamp(selection["effectiveAt"])
            harness = selection["harness"]
            if harness in last and stamp < last[harness]:
                raise DomainError(
                    "LEGACY_SELECTION_ORDER_INVALID",
                    "同一 Harness 的价格选择时间线逆序",
                    f"selections[{index}].effectiveAt",
                )
            last[harness] = stamp
        selections.sort(key=lambda item: parse_instant(item["effectiveAt"]))
        if not any(item["harness"] == "dsh" for item in selections):
            enabled = [group["id"] for group in groups if not group["disabled"]]
            chosen = next(
                (item["groupId"] for item in reversed(selections) if item["groupId"] in enabled), None
            )
            chosen = chosen or (enabled[0] if enabled else None)
            if chosen is not None:
                times = [parse_instant(item["effectiveAt"]) for item in selections]
                for group in groups:
                    times.extend(parse_instant(item["effectiveAt"]) for item in group.get("activations", []))
                    for version in group["versions"]:
                        times.append(parse_instant(version["effectiveAt"]))
                        times.extend(
                            parse_instant(item["effectiveAt"]) for item in version.get("revisions", [])
                        )
                selections.append(
                    {"harness": "dsh", "groupId": chosen, "effectiveAt": format_instant(max(times))}
                )
        vendors = deepcopy(extension.get("vendors", []))
        vendor_ids = {vendor["id"] for vendor in vendors}
        for vendor in vendors:
            vendor.setdefault("topUps", [])
            vendor.setdefault("balanceCalibrations", [])
            vendor.setdefault(
                "createdAt",
                min((item["at"] for item in vendor["topUps"]), key=parse_instant, default=format_instant(at)),
            )
        current_links = {item["groupId"]: item["vendorId"] for item in extension.get("groupVendors", [])}
        version_links = {item["versionId"]: item["vendorId"] for item in extension.get("versionVendors", [])}
        links: dict[str, str] = {}
        for group in groups:
            linked = current_links.get(group["id"])
            if linked not in vendor_ids:
                linked = next(
                    (
                        version_links[v["id"]]
                        for v in reversed(group["versions"])
                        if version_links.get(v["id"]) in vendor_ids
                    ),
                    None,
                )
            if linked is not None:
                links[group["id"]] = linked
        multipliers = {
            (item["versionId"], model_key(item["modelId"])): item["multiplier"]
            for item in extension.get("modelMultipliers", [])
        }
        revision = snapshot.rows("configuration")[0]["revision"]
        if extension.get("configurationRevision") == revision:
            main["activePage"] = extension.get("activePage", main.get("activePage", "codex"))
        filters = main.setdefault("filters", {})
        for name in ("dsh", "overview"):
            filters[name] = deepcopy(
                extension.get("filters", {}).get(name, {"preset": "today", "start": "", "end": ""})
            )
        main.setdefault("historyPrompted", True)
        main.setdefault("collapsed", False)
        main.setdefault("modelBasePrices", [])
        main.setdefault("excludedModels", [])
        main.setdefault("settings", {}).setdefault("showScanCountdown", False)
        if "modelPool" not in main:
            names: dict[str, str] = {}
            for group in groups:
                for version in group["versions"]:
                    for rate in version["models"]:
                        names.setdefault(model_key(rate["modelId"]), rate["modelId"])
            main["modelPool"] = list(names.values())
        return LegacyMerged(
            snapshot.identity.version, main, extension, tuple(selections), tuple(vendors), links, multipliers
        )
    except (KeyError, TypeError, ValueError) as error:
        raise DomainError("LEGACY_CONFIGURATION_INVALID", "旧配置字段无效", "configuration") from error
