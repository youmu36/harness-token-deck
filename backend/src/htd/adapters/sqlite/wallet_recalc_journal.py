"""One quiet adjustment per repricing batch, supplier and original business day."""

import sqlite3
from collections import defaultdict
from typing import Any
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.wallet_closing import timezone
from htd.application.ports import UnitOfWork
from htd.application.ports.maintenance import RecalcUpdate
from htd.domain.common.time import Instant, format_instant
from htd.domain.pricing.history import HISTORY_GROUP_ID


def install(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS wallet_recalc_pending("
        "batch_id TEXT NOT NULL REFERENCES recalc_batch(id),vendor_id TEXT NOT NULL REFERENCES vendor(id),"
        "day TEXT NOT NULL,occurred_at TEXT NOT NULL,received_at TEXT NOT NULL,"
        "c TEXT NOT NULL,note TEXT NOT NULL,"
        "PRIMARY KEY(batch_id,vendor_id,day)) STRICT, WITHOUT ROWID"
    )
    db.execute("CREATE INDEX IF NOT EXISTS wallet_recalc_pending_vendor ON wallet_recalc_pending(vendor_id)")


def money(pico: int) -> str:
    whole, fraction = divmod(abs(pico), 10**12)
    tail = f"{fraction:012d}".rstrip("0")
    return ("-" if pico < 0 else "") + str(whole) + ("." + tail if tail else "")


def stage(
    db: sqlite3.Connection, work: UnitOfWork, updates: tuple[RecalcUpdate, ...], batch: str, at: Instant
) -> set[str]:
    groups: dict[tuple[str, str], list[RecalcUpdate]] = defaultdict(list)
    zone = ZoneInfo(timezone(db))
    for update in updates:
        original = update.row.consumption
        if original is not None and update.new["price_group_id"] != HISTORY_GROUP_ID:
            day = original.occurred_at.value.astimezone(zone).date().isoformat()
            groups[original.vendor_id, day].append(update)
    labels: dict[tuple[object, object], str] = {}

    def label(values: dict[str, Any]) -> str:
        key = values["price_group_id"], values["price_version_id"]
        if key not in labels:
            group = db.execute("SELECT name FROM price_group WHERE id=?", (key[0],)).fetchone()
            version = db.execute("SELECT number FROM price_version WHERE id=?", (key[1],)).fetchone()
            labels[key] = f"{group[0] if group else '未选价格组'}/版本{version[0] if version else '未知'}"
        return labels[key]

    for (vendor, day), rows in groups.items():
        before = sum(int(str(u.row.old["pico"])) for u in rows)
        after = sum(int(str(u.new["pico"])) for u in rows)
        effective = sum(
            0 if u.row.local_override else int(str(u.new["pico"])) - int(str(u.row.old["pico"])) for u in rows
        )

        transitions = sorted({label(u.row.old) + " → " + label(u.new) for u in rows})
        note = f"重算调整 {len(rows)} 条，估算消费 {money(before)} → {money(after)} USD，" + "，".join(
            transitions[:6]
        )
        if len(transitions) > 6:
            note += f"，另 {len(transitions) - 6} 类变动详见重算审计"
        if any(u.row.local_override for u in rows):
            note += f"，保留人工校准，实际消费变化 {money(effective)} USD"
        occurred = max(u.row.event.occurred_at for u in rows)
        db.execute(
            "INSERT INTO wallet_recalc_pending VALUES(?,?,?,?,?,?,?)",
            (batch, vendor, day, occurred, format_instant(at), str(effective), note),
        )
        work.touch("wallet")
    return {vendor for vendor, _ in groups}
