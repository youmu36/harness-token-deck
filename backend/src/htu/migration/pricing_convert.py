"""Validate legacy prices and map them into the new historical catalog."""

from collections.abc import Callable
from dataclasses import asdict
from functools import partial
from typing import Any

from htu.domain.common.errors import DomainError
from htu.domain.common.time import Instant, format_instant, parse_instant
from htu.domain.pricing.catalog import Catalog
from htu.domain.pricing.model import (
    BillingRule,
    EditorHints,
    HarnessSelection,
    ModelRate,
    PriceGroup,
    PriceRates,
    PriceRevision,
    PriceVersion,
    VersionActivation,
)
from htu.domain.pricing.model_pool import BasePrice, ModelPool
from htu.domain.usage.model import Harness
from htu.domain.usage.model_key import model_key
from htu.migration.legacy.merge import LegacyMerged
from htu.migration.wallet_convert import stable_id


def _field[T](path: str, build: Callable[[], T]) -> T:
    try:
        return build()
    except DomainError as error:
        raise DomainError(
            error.code, error.message, path + ("." + error.field if error.field else "")
        ) from error
    except (KeyError, TypeError, ValueError) as error:
        raise DomainError("LEGACY_CONFIGURATION_INVALID", "旧配置字段无效", path) from error


def model(data: dict[str, Any]) -> ModelRate:
    rule = data.get("billing")
    billing = (
        None
        if rule is None
        else BillingRule(
            rule["mode"],
            PriceRates(**rule["rates"]),
            tuple(rule["weekdays"]),
            tuple(rule["hours"]),
            rule["offsetMinutes"],
            rule["thresholdK"],
        )
    )
    return ModelRate(
        data["modelId"],
        data["displayName"],
        data.get("input"),
        data.get("cache"),
        data.get("output"),
        billing,
    )


def catalog_from_legacy(merged: LegacyMerged, *, at: Instant) -> Catalog:
    groups: list[PriceGroup] = []
    versions: list[PriceVersion] = []
    revisions: list[PriceRevision] = []
    activations: list[VersionActivation] = []
    selections: list[HarnessSelection] = []
    hints: list[EditorHints] = []
    seq = 0
    for gi, group in enumerate(merged.main["groups"]):
        gp = f"groups[{gi}]"
        created = min((parse_instant(v["effectiveAt"]) for v in group["versions"]), default=at)
        groups.append(_field(gp, partial(PriceGroup, group["id"], group["name"], group["disabled"], created)))
        for vi, version in enumerate(group["versions"]):
            vp = f"{gp}.versions[{vi}]"
            version_at = _field(vp + ".effectiveAt", partial(parse_instant, version["effectiveAt"]))
            versions.append(
                _field(
                    vp,
                    partial(
                        PriceVersion,
                        version["id"],
                        group["id"],
                        version["number"],
                        version.get("label", ""),
                        version_at,
                    ),
                )
            )
            current = tuple(
                _field(f"{vp}.models[{mi}]", lambda rate=rate: model(rate))
                for mi, rate in enumerate(version["models"])
            )
            history = version.get("revisions") or [
                {"effectiveAt": version["effectiveAt"], "models": version["models"]}
            ]
            previous: Instant | None = None
            for ri, revision in enumerate(history):
                rp = f"{vp}.revisions[{ri}]"
                effective = _field(rp + ".effectiveAt", partial(parse_instant, revision["effectiveAt"]))
                if (ri == 0 and effective != version_at) or (previous is not None and effective < previous):
                    raise DomainError(
                        "LEGACY_CONFIGURATION_INVALID", "修订起点或时间顺序无效", rp + ".effectiveAt"
                    )
                seq += 1
                rates = tuple(
                    _field(f"{rp}.models[{mi}]", lambda rate=rate: model(rate))
                    for mi, rate in enumerate(revision["models"])
                )
                record = _field(
                    rp,
                    partial(
                        PriceRevision,
                        stable_id("price_revision", f"{version['id']}:{ri}"),
                        version["id"],
                        effective,
                        seq,
                        rates,
                    ),
                )
                revisions.append(record)
                previous = effective
            if sorted(current, key=lambda rate: rate.key) != list(revisions[-1].models):
                raise DomainError("LEGACY_CONFIGURATION_INVALID", "末条修订与当前模型不一致", vp + ".models")
            for rate in current:
                multiplier = merged.multipliers.get((version["id"], model_key(rate.model_id)), "1")
                hints.append(
                    _field(vp + ".multiplier", partial(EditorHints, version["id"], rate.model_id, multiplier))
                )
        previous = None
        for ai, activation in enumerate(group.get("activations", [])):
            ap = f"{gp}.activations[{ai}]"
            effective = _field(ap, partial(parse_instant, activation["effectiveAt"]))
            if previous is not None and effective < previous:
                raise DomainError("LEGACY_CONFIGURATION_INVALID", "激活时间线逆序", ap)
            seq += 1
            activations.append(
                _field(ap, partial(VersionActivation, group["id"], activation["versionId"], effective, seq))
            )
            previous = effective
    for si, selection in enumerate(merged.selections):
        seq += 1
        selections.append(
            _field(
                f"selections[{si}]",
                partial(
                    HarnessSelection,
                    Harness(selection["harness"]),
                    selection["groupId"],
                    parse_instant(selection["effectiveAt"]),
                    seq,
                ),
            )
        )
    if not groups:
        raise DomainError("LEGACY_CONFIGURATION_INVALID", "旧配置至少需要一个价格组", "groups")
    return _field(
        "pricing",
        lambda: Catalog(
            tuple(groups),
            tuple(versions),
            tuple(revisions),
            tuple(activations),
            tuple(selections),
            tuple(hints),
        ),
    )


def models_from_legacy(merged: LegacyMerged) -> ModelPool:
    return _field(
        "modelPool",
        lambda: ModelPool(
            tuple(merged.main["modelPool"]),
            tuple(merged.main["excludedModels"]),
            tuple(
                BasePrice(
                    base["modelId"], PriceRates(base.get("input"), base.get("cache"), base.get("output"))
                )
                for base in merged.main["modelBasePrices"]
            ),
        ),
    )


def price_mapping(merged: LegacyMerged, catalog: Catalog) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for group in merged.main["groups"]:
        for version in group["versions"]:
            history = version.get("revisions") or [version]
            for index, original in enumerate(history):
                revision_id = stable_id("price_revision", f"{version['id']}:{index}")
                revision = next(record for record in catalog.revisions if record.id == revision_id)
                result.append(
                    {
                        "groupId": group["id"],
                        "versionId": version["id"],
                        "revisionId": revision_id,
                        "effectiveAt": format_instant(revision.effective_at),
                        "originalModels": original["models"],
                        "normalizedModels": [asdict(rate) for rate in revision.models],
                    }
                )
    return tuple(result)
