"""Encode pricing snapshots and replayable switch receipts with stable JSON."""

import hashlib
import json
from dataclasses import asdict
from typing import Any, cast

from htu.domain.common.time import format_instant, parse_instant
from htu.domain.pricing.model import (
    BillingRule,
    HarnessSelection,
    ModelRate,
    PriceRates,
    SwitchPlan,
    VersionActivation,
)
from htu.domain.usage.model import Harness


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def rule_json(model: ModelRate) -> str | None:
    return dumps(asdict(model.billing_rule)) if model.billing_rule else None


def decode_rule(text: str | None) -> BillingRule | None:
    if text is None:
        return None
    data = cast(dict[str, Any], json.loads(text))
    return BillingRule(
        data["mode"],
        PriceRates(**data["rates"]),
        tuple(data["weekdays"]),
        tuple(data["hours"]),
        data["offset_minutes"],
        data["threshold_k"],
    )


def fingerprint(models: tuple[ModelRate, ...]) -> str:
    return hashlib.sha256(dumps([asdict(model) for model in models]).encode("utf-8")).hexdigest()


def encode_switch(plan: SwitchPlan) -> str:
    return dumps(
        {
            "operation_id": plan.operation_id,
            "harness": plan.harness.value,
            "group_id": plan.group_id,
            "version_id": plan.version_id,
            "expected_revision": plan.expected_revision,
            "effective_at": format_instant(plan.effective_at),
            "selections": [
                (s.harness.value, s.group_id, format_instant(s.effective_at), s.seq) for s in plan.selections
            ],
            "activations": [
                (a.group_id, a.version_id, format_instant(a.effective_at), a.seq) for a in plan.activations
            ],
            "affected_harnesses": [h.value for h in plan.affected_harnesses],
        }
    )


def decode_switch(text: str) -> SwitchPlan:
    data = cast(dict[str, Any], json.loads(text))
    return SwitchPlan(
        data["operation_id"],
        Harness(data["harness"]),
        data["group_id"],
        data["version_id"],
        data["expected_revision"],
        parse_instant(data["effective_at"]),
        tuple(
            HarnessSelection(Harness(h), g, parse_instant(at), seq) for h, g, at, seq in data["selections"]
        ),
        tuple(VersionActivation(g, v, parse_instant(at), seq) for g, v, at, seq in data["activations"]),
        tuple(Harness(h) for h in data["affected_harnesses"]),
    )
