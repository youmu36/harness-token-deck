"""Repair only proven DSH missing-cache costs at their original frozen prices."""

import hashlib
import sqlite3
from datetime import timedelta

from htu.adapters.sqlite.recalc_command_store import SqliteRecalcStore
from htu.application.ports import UnitOfWork, UsageChanged, WalletChanged
from htu.application.ports.maintenance import RecalcUpdate
from htu.domain.billing import estimate_cost
from htu.domain.common.time import Instant, format_instant, parse_instant
from htu.domain.pricing.catalog import Resolution
from htu.domain.pricing.history import HISTORY_GROUP_ID
from htu.domain.usage.model import Harness


def repair(db: sqlite3.Connection, work: UnitOfWork, at: Instant, limit: int = 50) -> int:
    candidates = db.execute(
        "SELECT l.dedupe_key,l.price_group_id,l.price_version_id FROM usage_annotation a "
        "JOIN ledger l USING(harness,dedupe_key) WHERE a.harness='dsh' AND a.cache_read IS NOT NULL "
        "AND l.cache_read IS NULL AND l.input IS NOT NULL AND l.output IS NOT NULL "
        "AND l.incomplete=1 AND l.unpriced=0 AND l.price_revision_id IS NOT NULL "
        "AND l.price_group_id<>? LIMIT ?",
        (HISTORY_GROUP_ID, limit),
    ).fetchall()
    if not candidates:
        return 0
    groups: dict[tuple[str, str], list[str]] = {}
    for row in candidates:
        if row["price_group_id"] != HISTORY_GROUP_ID:
            groups.setdefault((row["price_group_id"], row["price_version_id"]), []).append(row["dedupe_key"])
    repaired = 0
    for (group, version), keys in groups.items():
        snapshot = SqliteRecalcStore.snapshot_rows(
            db,
            Harness.DSH,
            parse_instant("1971-01-01T00:00:00.000Z"),
            Instant(at.value + timedelta(minutes=1)),
            keys=tuple(keys),
        )
        updates: list[RecalcUpdate] = []
        for row in snapshot.rows:
            revision = next(
                (r for r in snapshot.catalog.revisions if r.id == row.old["price_revision_id"]), None
            )
            if revision is None:
                continue
            cost = estimate_cost(
                row.event.tokens,
                Resolution(revision=revision).rate_for(row.event.model_id),
                parse_instant(row.event.occurred_at),
            )
            if cost.incomplete or cost.unpriced:
                continue
            updates.append(
                RecalcUpdate(
                    row,
                    {
                        **row.old,
                        "pico": str(cost.pico_usd),
                        "incomplete": 0,
                        "unpriced": 0,
                        "rule_applied": cost.rule_applied,
                        "billed_at": format_instant(at),
                    },
                )
            )
        if not updates:
            continue
        fingerprint = hashlib.sha256(
            repr([(u.row.event.dedupe_key, u.row.old) for u in updates]).encode()
        ).hexdigest()
        _, vendors = SqliteRecalcStore.commit_recalc(
            work,
            {
                "harness": "dsh",
                "start": min(u.row.event.occurred_at for u in updates),
                "end": format_instant(
                    Instant(
                        max(parse_instant(u.row.event.occurred_at).value for u in updates)
                        + timedelta(milliseconds=1)
                    )
                ),
                "groupId": group,
                "versionId": version,
                "fingerprint": fingerprint,
            },
            tuple(updates),
            "dsh-cache-repair:" + fingerprint,
            at=at,
        )
        repaired += len(updates)
        work.emit(UsageChanged((Harness.DSH,), work.revision("usage")))
        if vendors:
            work.emit(WalletChanged(vendors, work.revision("wallet")))
    return repaired
