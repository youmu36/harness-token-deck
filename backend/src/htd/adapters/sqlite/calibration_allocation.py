"""User-selected exact allocation of a difference; no automatic financial writes."""

import sqlite3
from collections import defaultdict
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.reconciliation_read import member_rows
from htd.adapters.sqlite.wallet_closing import timezone
from htd.domain.common.errors import DomainError
from htd.domain.common.time import parse_instant
from htd.domain.wallet.calibration import nonnegative_pico


def split(amount: int, weights: list[int]) -> list[int]:
    total = sum(weights)
    if not total:
        if amount:
            raise DomainError("CALIBRATION_WEIGHTS", "没有可分配的消费比例，请人工指定金额")
        return [0] * len(weights)
    sign = -1 if amount < 0 else 1
    values = [abs(amount) * weight // total for weight in weights]
    remainder = abs(amount) - sum(values)
    order = sorted(range(len(weights)), key=lambda i: (-(abs(amount) * weights[i] % total), i))
    for i in order[:remainder]:
        values[i] += 1
    return [sign * value for value in values]


def expand(db: sqlite3.Connection, query: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    mode = proposal.get("allocationMode", "explicit")
    if mode == "explicit" or proposal.get("action") == "revoke":
        return proposal
    totals: dict[tuple[str, str], int] = defaultdict(int)
    zone = ZoneInfo(timezone(db))
    for member in member_rows(db, query):
        day = parse_instant(member["occurred_at"]).value.astimezone(zone).date().isoformat()
        totals[day, member["model_key"]] += int(member["estimate_pico"])
        if len(totals) > 100:
            raise DomainError("CALIBRATION_ALLOCATION_LIMIT", "日期与模型分项超过100项，请缩小校准时间范围")
    keys = sorted(totals)
    target = nonnegative_pico(proposal["targetPico"])
    delta = target - sum(totals.values())
    weights = [totals[key] for key in keys]
    if mode == "weighted":
        models = sorted({model for _, model in keys})
        supplied = proposal.get("modelWeights", {})
        if set(supplied) != set(models):
            raise DomainError("CALIBRATION_WEIGHTS", "请为当前时段每个模型指定比例")
        model_weights = [int(Decimal(supplied[model]) * 10000) for model in models]
        if sum(model_weights) != 10000 or min(model_weights) < 0:
            raise DomainError("CALIBRATION_WEIGHTS", "模型比例之和须为1，每项为0到1")
        model_deltas = dict(zip(models, split(delta, model_weights), strict=True))
        adjustments = dict.fromkeys(keys, 0)
        for model in models:
            selected = [key for key in keys if key[1] == model]
            shares = split(model_deltas[model], [totals[key] for key in selected])
            adjustments.update(zip(selected, shares, strict=True))
        differences = [adjustments[key] for key in keys]
    else:
        differences = split(delta, weights)
    days: dict[str, int] = defaultdict(int)
    allocations: list[dict[str, Any]] = []
    for (day, model), difference in zip(keys, differences, strict=True):
        amount = totals[day, model] + difference
        if amount < 0:
            raise DomainError("CALIBRATION_NEGATIVE_MODEL", "该比例会使模型消费为负，请调整比例")
        days[day] += amount
        if mode != "unallocated":
            if not model:
                raise DomainError("CALIBRATION_MODEL_COVERAGE", "包含未知模型，请选择仅校准总额")
            allocations.append(
                {"day": day, "modelKeys": [model], "targetPico": str(amount), "confirmModelCoverage": True}
            )
    return dict(
        proposal,
        dayTargets=[{"day": day, "targetPico": str(value)} for day, value in days.items()],
        allocations=allocations,
    )
