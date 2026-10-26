"""Original voucher wins; otherwise resolve the link effective at completion."""

import sqlite3

VENDOR_SQL = (
    "COALESCE(w.vendor_id,(SELECT vl.vendor_id FROM vendor_link vl WHERE "
    "vl.group_id=l.price_group_id AND vl.effective_at<=l.occurred_at "
    "ORDER BY vl.effective_at DESC,vl.seq DESC LIMIT 1),'')"
)


def vendor_for_request(
    db: sqlite3.Connection, harness: str, key: bytes, group: str | None, stamp: str
) -> str:
    voucher = db.execute(
        "SELECT vendor_id FROM wallet_entry WHERE usage_harness=? AND usage_key=? AND kind='consumption'",
        (harness, key),
    ).fetchone()
    if voucher:
        return str(voucher[0])
    linked = db.execute(
        "SELECT vendor_id FROM vendor_link WHERE group_id=? AND effective_at<=? "
        "ORDER BY effective_at DESC,seq DESC LIMIT 1",
        (group, stamp),
    ).fetchone()
    return str(linked[0] or "") if linked else ""
