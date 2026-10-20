"""Version-specific legacy configuration JSON and its derived price tables."""

import sqlite3
from typing import Any

from scripts.legacy_fixtures.schema import at, compact, insert


def model(input_rate: str, *, other: bool = False) -> dict[str, Any]:
    return {
        "modelId": "legacy-other" if other else "legacy-model",
        "displayName": "Legacy Other" if other else "Legacy Model",
        "input": input_rate,
        "cache": "0.25",
        "output": "5" if other else "2",
    }


def main_state(version: str) -> dict[str, Any]:
    initial, revised = [model("3")], [model("4.000000")]
    second: dict[str, Any] = {
        "id": "a-v2",
        "number": 2,
        "effectiveAt": at(3),
        "models": revised,
        "revisions": [{"effectiveAt": at(3), "models": initial}, {"effectiveAt": at(4), "models": revised}],
    }
    groups: list[dict[str, Any]] = [
        {
            "id": "group-a",
            "name": "Primary",
            "disabled": False,
            "versions": [
                {"id": "a-v1", "number": 1, "effectiveAt": at(1), "models": [model("1.00")]},
                second,
            ],
        },
        {
            "id": "group-b",
            "name": "Secondary",
            "disabled": False,
            "versions": [
                {"id": "b-v1", "number": 1, "effectiveAt": at(1), "models": [model("2", other=True)]},
            ],
        },
    ]
    state: dict[str, Any] = {
        "schemaVersion": 1,
        "activePage": "prices",
        "collapsed": version == "v023",
        "settings": {"retention": "90", "timezone": "UTC", "theme": "dark"},
        "groups": groups,
        # Globally out of order, but each harness timeline is nondecreasing.
        "selections": [
            {"harness": "codex", "groupId": "group-a", "effectiveAt": at(1)},
            {"harness": "codex", "groupId": "group-a", "effectiveAt": at(3)},
            {"harness": "zcode", "groupId": "group-b", "effectiveAt": at(1)},
        ],
        "filters": {
            "codex": {"preset": "custom", "start": "2026-09-01T08:00", "end": "2026-09-09T08:00"},
            "zcode": {"preset": "7d", "start": "", "end": ""},
        },
    }
    if version != "v022":
        state.update(
            modelPool=["legacy-model", "legacy-other"], modelBasePrices=[], excludedModels=["removed-model"]
        )
        second["label"] = "Updated price"
    if version == "v030":
        state["historyPrompted"] = False
        state["settings"]["showScanCountdown"] = True
        state["modelBasePrices"] = [
            {"modelId": "legacy-model", "input": "1.50", "cache": None, "output": "0"}
        ]
        for group in groups:
            group["activations"] = [
                {"versionId": v["id"], "effectiveAt": v["effectiveAt"]} for v in group["versions"]
            ]
        peak = model("1")
        peak.update(
            modelId="peak-model",
            displayName="Peak Model",
            billing={
                "mode": "peak",
                "rates": {"input": "2", "cache": None, "output": "0"},
                "weekdays": [1, 2, 3, 4, 5],
                "hours": [9, 10],
                "offsetMinutes": 480,
                "thresholdK": 128,
            },
        )
        tier = model("2")
        tier.update(
            modelId="tier-model",
            displayName="Tier Model",
            billing={
                "mode": "tier",
                "rates": {"input": "3", "cache": "0", "output": "6"},
                "weekdays": [6, 7],
                "hours": [0],
                "offsetMinutes": -300,
                "thresholdK": 128,
            },
        )
        groups[1]["versions"][0]["models"].extend([peak, tier])
        state["modelPool"].extend(["peak-model", "tier-model"])
    return state


def extension(version: str) -> dict[str, Any] | None:
    if version == "v022":
        return None
    result: dict[str, Any] = {
        "configurationRevision": 23 if version == "v023" else 30,
        "activePage": "overview",
        "vendors": [
            {"id": "vendor-alpha", "name": "Alpha", "topUps": [{"at": at(1), "amount": "100"}]},
            {
                "id": "vendor-beta",
                "name": "Beta",
                "createdAt": at(1),
                "topUps": [{"at": at(1), "amount": "50"}],
            },
        ],
        "versionVendors": [
            {"versionId": "a-v1", "vendorId": "vendor-alpha"},
            {"versionId": "a-v2", "vendorId": "vendor-beta"},
            {"versionId": "b-v1", "vendorId": "vendor-alpha"},
        ],
        "filters": {
            "dsh": {"preset": "30d", "start": "", "end": ""},
            "overview": {"preset": "today", "start": "", "end": ""},
        },
        "dshSelections": [{"harness": "dsh", "groupId": "group-a", "effectiveAt": at(3)}],
    }
    if version == "v023":
        # The withdrawn schema stored DSH selections in the main derived table.
        result["dshSelections"] = []
    else:
        result["vendors"] = [
            {
                "id": "vendor-alpha",
                "name": "Alpha",
                "createdAt": at(1),
                "topUps": [
                    {"at": at(1), "amount": "100"},
                    {"at": at(2), "amount": "20"},
                    {"at": at(3), "amount": "5"},
                    {"at": at(6), "amount": "10.25"},
                ],
                "balanceCalibrations": [{"at": at(2), "balance": "80"}, {"at": at(3), "balance": "60.5"}],
            },
            {
                "id": "vendor-beta",
                "name": "Beta",
                "topUps": [
                    {"at": at(2), "amount": "10000.000001"},
                    {"at": at(5), "amount": "0.5"},
                ],
            },
            {
                "id": "vendor-gamma",
                "name": "Gamma",
                "createdAt": at(1),
                "topUps": [],
                "balanceCalibrations": [{"at": at(4), "balance": "-0.5"}],
            },
            {"id": "vendor-empty", "name": "Empty", "topUps": []},
        ]
        result["groupVendors"] = [
            {"groupId": "group-a", "vendorId": "vendor-alpha"},
            {"groupId": "group-b", "vendorId": "vendor-beta"},
        ]
        result["modelMultipliers"] = [{"versionId": "a-v2", "modelId": "legacy-model", "multiplier": "1.750"}]
    return result


def write_prices(
    db: sqlite3.Connection, state: dict[str, Any], ext: dict[str, Any] | None, version: str
) -> None:
    for group in state["groups"]:
        insert(
            db, "price_groups", {"id": group["id"], "name": group["name"], "disabled": int(group["disabled"])}
        )
        for price in group["versions"]:
            insert(
                db,
                "price_versions",
                {
                    "id": price["id"],
                    "group_id": group["id"],
                    "number": price["number"],
                    "label": price.get("label", ""),
                    "effective_at": price["effectiveAt"],
                },
            )
            for rate in price["models"]:
                insert(
                    db,
                    "price_model_rates",
                    {
                        "version_id": price["id"],
                        "model_id": rate["modelId"],
                        "display_name": rate["displayName"],
                        "input": rate["input"],
                        "cache": rate["cache"],
                        "output": rate["output"],
                        "billing_json": compact(rate["billing"]) if "billing" in rate else None,
                    },
                )
    selections = list(state["selections"])
    if version == "v023":
        selections.append({"harness": "dsh", "groupId": "group-a", "effectiveAt": at(3)})
    for index, selection in enumerate(selections, 1):
        insert(
            db,
            "price_selections",
            {
                "id": index,
                "harness": selection["harness"],
                "group_id": selection["groupId"],
                "effective_at": selection["effectiveAt"],
            },
        )
    for index, selection in enumerate(ext.get("dshSelections", []) if ext else [], 1):
        insert(
            db,
            "dsh_price_selections",
            {
                "id": index,
                "harness": "dsh",
                "group_id": selection["groupId"],
                "effective_at": selection["effectiveAt"],
            },
        )
