"""Generate deterministic synthetic legacy databases from docs/spec/legacy-data.md."""

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.legacy_fixtures.config import extension, main_state, write_prices
from scripts.legacy_fixtures.schema import (
    EXTENSION_SCHEMA,
    ROOT,
    SCHEMA,
    SNAPSHOT_AT,
    VERSIONS,
    compact,
    insert,
    pico,
)
from scripts.legacy_fixtures.usage import ledger, write_audit, write_collection


def old_balances(
    state: dict[str, Any], ext: dict[str, Any] | None, rows: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Independent oracle: spec/migration.md wallet step 1; never call the new wallet/billing code."""
    if ext is None:
        return {}
    vendors = {vendor["id"]: vendor for vendor in ext["vendors"]}
    group_links = {link["groupId"]: link["vendorId"] for link in ext.get("groupVendors", [])}
    version_links = {link["versionId"]: link["vendorId"] for link in ext.get("versionVendors", [])}
    associations: dict[str, str | None] = {}
    for group in state["groups"]:
        vendor_id = group_links.get(group["id"])
        if vendor_id not in vendors:
            vendor_id = next(
                (
                    version_links[v["id"]]
                    for v in reversed(group["versions"])
                    if version_links.get(v["id"]) in vendors
                ),
                None,
            )
        associations[group["id"]] = vendor_id
    result: dict[str, dict[str, Any]] = {}
    for vendor_id, vendor in vendors.items():
        top_ups = vendor["topUps"]
        created = vendor.get("createdAt") or min((t["at"] for t in top_ups), default=SNAPSHOT_AT)
        calibrations = vendor.get("balanceCalibrations", [])
        latest = calibrations[-1] if calibrations else None
        anchor = latest["at"] if latest else created
        anchor_balance = pico(latest["balance"]) if latest else 0

        # A calibration excludes equal timestamps; creation includes them.
        def after(timestamp: str, bound: str = anchor, strict: bool = latest is not None) -> bool:
            return timestamp > bound if strict else timestamp >= bound

        groups = [group_id for group_id, linked in associations.items() if linked == vendor_id]
        recharge = sum(pico(t["amount"]) for t in top_ups if after(t["at"]))
        consumption = sum(
            int(row["pico_usd"])
            for row in rows
            if row["price_group_id"] in groups and after(row["occurred_at"])
        )
        result[vendor_id] = {
            "createdAt": created,
            "groupIds": groups,
            "anchorAt": anchor,
            "anchorInclusive": latest is None,
            "anchorPicoUsd": str(anchor_balance),
            "topUpsAfterAnchorPicoUsd": str(recharge),
            "consumptionAfterAnchorPicoUsd": str(consumption),
            "topUpsPicoUsd": str(sum(pico(t["amount"]) for t in top_ups)),
            "balancePicoUsd": str(anchor_balance + recharge - consumption),
        }
    return result


def build_database(path: Path, version: str) -> dict[str, Any]:
    state, ext, rows = main_state(version), extension(version), ledger(version)
    temporary = path.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    db = sqlite3.connect(temporary)
    try:
        db.execute("PRAGMA page_size=4096")
        db.execute("PRAGMA foreign_keys=ON")
        # A closed, standalone fixture has no nondeterministic WAL/SHM sidecars.
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute(f"PRAGMA user_version={7 if version == 'v023' else 6}")
        db.executescript(SCHEMA)
        if ext is not None:
            db.executescript(EXTENSION_SCHEMA)
            insert(
                db,
                "schema_metadata",
                {
                    "id": 1,
                    "storage_generation": 6,
                    "current_schema": "0.2.3-compat-1" if version == "v023" else "0.2.4-compat-1",
                },
            )
            insert(
                db,
                "configuration_extensions",
                {"id": 1, "configuration_revision": ext["configurationRevision"], "state_json": compact(ext)},
            )
        insert(
            db,
            "configuration",
            {
                "id": 1,
                "revision": {"v022": 22, "v023": 23, "v030": 30}[version],
                "state_json": compact(state),
                "updated_at": SNAPSHOT_AT,
            },
        )
        write_prices(db, state, ext, version)
        for row in rows:
            insert(db, "usage_ledger", row)
            insert(
                db,
                "usage_identities",
                {
                    "harness": row["harness"],
                    "dedupe_key": row["dedupe_key"],
                    "occurred_at": row["occurred_at"],
                    "billed": 1,
                },
            )
        write_collection(db, version)
        write_audit(db, rows, version)
        db.commit()
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = [
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
    finally:
        db.close()
    temporary.replace(path)
    totals: defaultdict[str, int] = defaultdict(int)
    daily: defaultdict[str, int] = defaultdict(int)
    for row in rows:
        totals[row["harness"]] += int(row["pico_usd"])
        daily[f"{row['harness']}:{row['occurred_at'][:10]}"] += int(row["pico_usd"])
    return {
        "sourceVersion": {"v022": "0.2.2", "v023": "0.2.3", "v030": "0.3.0"}[version],
        "userVersion": 7 if version == "v023" else 6,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "rowCounts": counts,
        "ledgerPicoUsd": str(sum(totals.values())),
        "harnessPicoUsd": {h: str(totals[h]) for h in ("codex", "zcode", "dsh")},
        "harnessDatePicoUsd": {date: str(total) for date, total in sorted(daily.items())},
        "vendors": old_balances(state, ext, rows),
    }


def generate(destination: Path) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=True)
    manifest = {
        "snapshotAt": SNAPSHOT_AT,
        "databases": {
            f"{version}.sqlite3": build_database(destination / f"{version}.sqlite3", version)
            for version in VERSIONS
        },
    }
    (destination / "expected.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "fixtures/legacy")
    args = parser.parse_args()
    manifest = generate(args.output_dir)
    for filename, expected in manifest["databases"].items():
        print(
            f"{filename}: ledger={expected['rowCounts']['usage_ledger']}, pico={expected['ledgerPicoUsd']}, "
            f"sha256={expected['sha256']}"
        )


if __name__ == "__main__":
    main()
