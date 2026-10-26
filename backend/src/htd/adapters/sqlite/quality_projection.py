"""Stable evidence sets survive retirement; acknowledgements never overwrite each other."""

import hashlib
import json
import sqlite3
from typing import Any

REASONS = {
    "version": "价格组/倍率版本缺失",
    "cache": "缓存覆盖不完整",
    "incomplete": "用量或计费字段不完整",
    "unpriced": "未定价",
}


class QualityProjection:
    def __init__(self) -> None:
        self.groups: dict[tuple[str, ...], dict[str, Any]] = {}

    def add(self, date: str, key: tuple[str, ...], name: str, values: dict[str, int]) -> None:
        records = values["records"]
        if records <= 0:
            return
        exact = values.get("quality_known", 0) == records
        counts = {reason: values.get("quality_" + reason, 0) for reason in REASONS}
        count = values.get("quality_count", 0)
        if not exact:
            # Old OR flags only prove a lower bound. Never multiply them by records.
            counts["version"] = records if not key[3] else counts["version"]
            counts["cache"] = max(counts["cache"], values.get("unknown", 0))
            counts["incomplete"] = max(counts["incomplete"], int(bool(values.get("incomplete"))))
            counts["unpriced"] = max(counts["unpriced"], int(bool(values.get("unpriced"))))
            count = max(count, *counts.values())
        if not count:
            return
        identity = date, *key
        item = self.groups.setdefault(
            identity,
            {
                "name": name,
                "count": 0,
                "signature": 0,
                "exact": True,
                "counts": dict.fromkeys(REASONS, 0),
                "legacy": [],
                "legacyOnly": True,
            },
        )
        item["count"] += count
        item["signature"] += values.get("quality_signature", 0)
        item["exact"] &= exact
        item["legacyOnly"] &= not exact
        if not exact:
            old_key = (*identity, name, *(label for reason, label in REASONS.items() if counts[reason]))
            old_id = hashlib.sha256(json.dumps(old_key, ensure_ascii=False).encode()).hexdigest()
            old_fingerprint = hashlib.sha256(f"{old_id}:{records}".encode()).hexdigest()
            item["legacy"].append((old_id, old_fingerprint))
        for reason, amount in counts.items():
            item["counts"][reason] += amount

    def export(self, db: sqlite3.Connection) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        for key, value in sorted(self.groups.items(), reverse=True):
            identifier = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()
            content = [identifier, value["count"], value["signature"], value["exact"], value["counts"]]
            fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
            issues.append(
                {
                    "id": identifier,
                    "fingerprint": fingerprint,
                    "date": key[0],
                    "harness": key[1],
                    "vendorId": key[2],
                    "groupId": key[3],
                    "versionId": key[4],
                    "modelKey": key[5],
                    "displayName": value["name"],
                    "reasons": [label for reason, label in REASONS.items() if value["counts"][reason]],
                    "reasonCounts": {
                        REASONS[reason]: str(count) for reason, count in value["counts"].items() if count
                    },
                    "recordCount": str(value["count"]),
                    "countAccuracy": "exact" if value["exact"] else "at_least",
                    "acknowledged": False,
                }
            )
        for index in range(0, len(issues), 400):
            batch = issues[index : index + 400]
            placeholders = ",".join("?" for _ in batch)
            acknowledged: set[tuple[str, str]] = {
                (row[0], row[1])
                for row in db.execute(
                    f"SELECT id,fingerprint FROM quality_ack_evidence WHERE id IN ({placeholders})",
                    [item["id"] for item in batch],
                )
            }
            for item in batch:
                item["acknowledged"] = (item["id"], item["fingerprint"]) in acknowledged
        # Only carry a legacy acknowledgement over identical lower-bound evidence.
        # A new exact missing request always reopens the combined group.
        legacy_groups = [value for value in self.groups.values() if value["legacyOnly"]]
        old_ids = list({identity for value in legacy_groups for identity, _ in value["legacy"]})
        old_ack: set[tuple[str, str]] = set()
        for index in range(0, len(old_ids), 400):
            batch = old_ids[index : index + 400]
            old_ack.update(
                (row[0], row[1])
                for row in db.execute(
                    "SELECT id,fingerprint FROM quality_notice_ack WHERE id IN ("
                    + ",".join("?" for _ in batch)
                    + ")",
                    batch,
                )
            )
        for item, value in zip(
            issues, (v for _, v in sorted(self.groups.items(), reverse=True)), strict=True
        ):
            if value["legacyOnly"] and all(tuple(pair) in old_ack for pair in value["legacy"]):
                item["acknowledged"] = True
        return issues
