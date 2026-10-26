"""Shared allocation basis, frozen ownership and valuation-only target maintenance."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from htd.domain.wallet.calibration import DIMENSIONS


@dataclass
class Basis:
    estimate: int = 0
    count: int = 0
    dimensions: dict[str, set[str]] = field(default_factory=lambda: {key: set() for key in DIMENSIONS})
    digest: Any = field(default_factory=hashlib.sha256)

    def add(self, member: sqlite3.Row) -> None:
        self.estimate += int(member["estimate_pico"])
        self.count += 1
        for key, name in DIMENSIONS.items():
            self.dimensions[key].add(member[name] or "")
        self.digest.update(json.dumps(tuple(member), ensure_ascii=True).encode())

    def scope(self) -> dict[str, list[str]]:
        return {key: sorted(values) for key, values in self.dimensions.items()}


def audit(
    db: sqlite3.Connection,
    target: str,
    kind: str,
    at: str,
    operation: str,
    before_estimate: int,
    after_estimate: int,
    before_delta: int,
    after_delta: int,
    note: str,
) -> None:
    db.execute(
        "INSERT INTO calibration_audit VALUES(NULL,?,?,?,?,?,?,?,?,?)",
        (
            target,
            kind,
            at,
            operation,
            str(before_estimate),
            str(after_estimate),
            str(before_delta),
            str(after_delta),
            note,
        ),
    )


def add_scope(db: sqlite3.Connection, bucket: str, member: sqlite3.Row, sign: int = 1) -> None:
    db.executemany(
        "INSERT INTO calibration_scope_count VALUES(?,?,?,?) ON CONFLICT(bucket_id,dimension,value) "
        "DO UPDATE SET count=count+excluded.count",
        ((bucket, dimension, member[name] or "", sign) for dimension, name in DIMENSIONS.items()),
    )


def refresh(db: sqlite3.Connection, harness: str, key: bytes, at: str, previous: sqlite3.Row | None) -> None:
    owner = db.execute(
        "SELECT c.target_id,c.bucket_id FROM calibration_owner o JOIN calibration_member c "
        "ON c.target_id=o.target_id AND c.harness=o.harness AND c.dedupe_key=o.dedupe_key "
        "WHERE o.harness=? AND o.dedupe_key=?",
        (harness, key),
    ).fetchone()
    if owner is None or previous is None:
        return
    member = db.execute(
        "SELECT * FROM reconciliation_member WHERE harness=? AND dedupe_key=?", (harness, key)
    ).fetchone()
    target = db.execute(
        "SELECT * FROM calibration_target WHERE id=? AND state='active'", (owner[0],)
    ).fetchone()
    if target is None:
        return
    delta_estimate = int(member["estimate_pico"]) - int(previous["estimate_pico"])
    changed_dimensions = [
        dimension for dimension, name in DIMENSIONS.items() if member[name] != previous[name]
    ]
    quality_delta = int(bool(member["incomplete"] or member["unpriced"])) - int(
        bool(previous["incomplete"] or previous["unpriced"])
    )
    if not delta_estimate and not changed_dimensions and not quality_delta:
        return
    bucket = db.execute("SELECT * FROM calibration_bucket WHERE id=?", (owner[1],)).fetchone()
    db.execute(
        "UPDATE calibration_bucket SET estimate_pico=htd_pico_add(estimate_pico,?),"
        "delta_pico=htd_pico_add(delta_pico,?),revision=revision+1 WHERE id=?",
        (str(delta_estimate), str(-delta_estimate), owner[1]),
    )
    if changed_dimensions or quality_delta:
        affected = db.execute(
            "SELECT * FROM calibration_bucket WHERE target_id=? AND (id=? OR (member_count=0 AND day=?))",
            (owner[0], owner[1], bucket["day"]),
        ).fetchall()
        for part in affected:
            if part["id"] != owner[1]:
                db.execute("UPDATE calibration_bucket SET revision=revision+1 WHERE id=?", (part["id"],))
            db.execute(
                "UPDATE calibration_scope_count SET count=count+? WHERE bucket_id=? "
                "AND dimension='eligibility' AND value='unavailable'",
                (quality_delta, part["id"]),
            )
            for dimension in changed_dimensions:
                name = DIMENSIONS[dimension]
                db.execute(
                    "UPDATE calibration_scope_count SET count=count-1 WHERE bucket_id=? AND "
                    "dimension=? AND value=?",
                    (part["id"], dimension, previous[name] or ""),
                )
                db.execute(
                    "INSERT INTO calibration_scope_count VALUES(?,?,?,1) ON "
                    "CONFLICT(bucket_id,dimension,value) DO UPDATE SET count=count+1",
                    (part["id"], dimension, member[name] or ""),
                )
            db.execute(
                "DELETE FROM calibration_scope_count WHERE bucket_id=? AND count=0 "
                "AND dimension<>'eligibility'",
                (part["id"],),
            )
            scope = json.loads(part["scope_json"])
            for dimension in changed_dimensions:
                scope[dimension] = [
                    row[0]
                    for row in db.execute(
                        "SELECT value FROM calibration_scope_count WHERE bucket_id=? AND dimension=? "
                        "ORDER BY value",
                        (part["id"], dimension),
                    )
                ]
            db.execute(
                "UPDATE calibration_bucket SET scope_json=? WHERE id=?",
                (json.dumps(scope, sort_keys=True), part["id"]),
            )
    estimate = int(target["estimate_pico"]) + delta_estimate
    delta = int(target["target_pico"]) - estimate
    revision = target["revision"] + 1
    audit(
        db,
        target["id"],
        "basis_changed",
        at,
        f"basis:{target['id']}:{revision}",
        int(target["estimate_pico"]),
        estimate,
        int(target["delta_pico"]),
        delta,
        "估值或归属变化，保留已确认目标",
    )
    db.execute(
        "UPDATE calibration_target SET estimate_pico=?,delta_pico=?,revision=? WHERE id=?",
        (str(estimate), str(delta), revision, target["id"]),
    )
    db.execute(
        "UPDATE calibration_member SET basis_revision=? WHERE target_id=? AND harness=? AND dedupe_key=?",
        (member["revision"], target["id"], harness, key),
    )
    db.execute("UPDATE calibration_revision SET value=value+1 WHERE id=1")
