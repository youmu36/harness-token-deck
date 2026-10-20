"""Independent local corrections; shared 0.4.1 vouchers remain untouched."""

import json
import sqlite3
from datetime import timedelta
from typing import Any, cast

from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.application.ports import UnitOfWork
from htu.domain.common.errors import DomainError
from htu.domain.common.time import Instant, format_instant
from htu.domain.wallet.vendors import require_active


def _usd(value: str | int) -> str:
    amount = int(value)
    rounded = (abs(amount) + 500_000) // 1_000_000
    whole, fraction = divmod(rounded, 10**6)
    tail = str(fraction).zfill(6).rstrip("0")
    return f"{'-' if amount < 0 and rounded else ''}{whole}" + ("." + tail if tail else "") + " USD"


def summary(db: sqlite3.Connection, vendor: str) -> dict[str, Any]:
    from htu.adapters.sqlite.wallet_inheritance import apply

    repo = WalletRepository(db)
    model, totals = repo.vendor(vendor), repo.totals(vendor)
    r, c, a = totals.r, totals.c, totals.a
    for row in db.execute("SELECT r,c,a FROM local_wallet_total WHERE vendor_id=?", (vendor,)):
        r += int(row[0])
        c += int(row[1])
        a += int(row[2])
    meta = db.execute("SELECT * FROM local_wallet_metadata WHERE vendor_id=?", (vendor,)).fetchone()
    return apply(
        db,
        vendor,
        "local",
        {
            "R": str(r),
            "C": str(c),
            "A": str(a),
            "B": str(r - c + a),
            "pastTopUpPico": meta["past_r"] if meta else str(model.past_top_up_pico),
            "pastConsumptionPico": meta["past_c"] if meta else str(model.past_consumption_pico),
        },
    )


def remote_summary(db: sqlite3.Connection, vendor: str) -> dict[str, Any] | None:
    from htu.adapters.sqlite.account_book import AccountBook
    from htu.adapters.sqlite.wallet_inheritance import apply, seed

    segments = db.execute(
        "SELECT * FROM account_segment WHERE vendor_id=? AND kind='remote' ORDER BY started_at,id", (vendor,)
    ).fetchall()
    if not segments:
        inherited = seed(db, vendor)
        return dict(inherited["target"]) if inherited else None
    r = sum(int(row["r"]) for row in segments)
    c = sum(int(row["c"]) for row in segments)
    a = sum(int(row["a"]) for row in segments)
    for row in db.execute("SELECT * FROM remote_wallet_correction_total WHERE vendor_id=?", (vendor,)):
        r += int(row["r"])
        c += int(row["c"])
        a += int(row["a"])
    for row in db.execute(
        "SELECT p.r,p.c,p.a FROM account_posting p JOIN account_segment s ON s.id=p.segment_id "
        "WHERE p.vendor_id=? AND s.kind='remote' AND p.kind='manual_top_up' AND p.interval_id IS NULL",
        (vendor,),
    ):
        r += int(row["r"])
        c += int(row["c"])
        a += int(row["a"])
    endpoint = AccountBook(db).latest(segments[-1]["id"])
    balance = int(endpoint["b"]) if endpoint else int(segments[-1]["initial_balance"])
    override = db.execute("SELECT * FROM account_balance_override WHERE vendor_id=?", (vendor,)).fetchone()
    if override and endpoint and override["endpoint_id"] == endpoint["id"]:
        balance = int(override["balance"])
    metadata = db.execute(
        "SELECT past_top_up,past_consumption FROM vendor_accounting WHERE vendor_id=?", (vendor,)
    ).fetchone()
    return apply(
        db,
        vendor,
        "remote",
        {
            "R": str(r),
            "C": str(c),
            "A": str(a),
            "B": str(balance),
            "pastTopUpPico": metadata[0] if metadata else "0",
            "pastConsumptionPico": metadata[1] if metadata else "0",
        },
    )


def post(
    db: sqlite3.Connection,
    work: UnitOfWork,
    vendor: str,
    at: Instant,
    operation: str,
    r: int,
    c: int,
    a: int,
    note: str,
    original: str | None = None,
    *,
    received: Instant | None = None,
    journal: bool = True,
) -> None:
    # The audit row precedes the changed projection; both commit with the receipt.
    db.execute(
        "INSERT INTO local_wallet_posting(vendor_id,occurred_at,r,c,a,note,original_id,operation_id) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (vendor, format_instant(at), str(r), str(c), str(a), note, original, operation),
    )
    old = db.execute("SELECT r,c,a FROM local_wallet_total WHERE vendor_id=?", (vendor,)).fetchone()
    db.execute(
        "INSERT INTO local_wallet_total VALUES(?,?,?,?) ON CONFLICT(vendor_id) DO UPDATE SET "
        "r=excluded.r,c=excluded.c,a=excluded.a",
        (
            vendor,
            str(r + (int(old[0]) if old else 0)),
            str(c + (int(old[1]) if old else 0)),
            str(a + (int(old[2]) if old else 0)),
        ),
    )
    from htu.adapters.sqlite.wallet_flows import stage

    if journal:
        stage(
            db,
            vendor,
            "local",
            "adjustment",
            format_instant(at),
            format_instant(received or at),
            r=r,
            c=c,
            a=a,
            note=note,
            work=work,
        )
    work.touch("wallet")


def set_totals(
    db: sqlite3.Connection,
    work: UnitOfWork,
    vendor: str,
    values: dict[str, object],
    operation: str,
    at: Instant,
) -> None:
    require_active(WalletRepository(db).vendor(vendor))
    if values["expectedWalletRevision"] != work.revision("wallet"):
        raise DomainError("WALLET_REVISION_CONFLICT", "本地钱包已变化，请重新读取后保存")
    before = summary(db, vendor)
    targets = {
        key: int(str(values[key] if values.get(key) is not None else before[key]))
        for key in ("R", "C", "pastTopUpPico", "pastConsumptionPico")
    }
    if any(value < 0 for value in targets.values()):
        raise DomainError("ACCOUNT_AMOUNT_INVALID", "累计与过往金额不能为负数")
    r, c = targets["R"] - int(before["R"]), targets["C"] - int(before["C"])
    a = int(str(values["B"])) - (int(before["B"]) + r - c) if values.get("B") is not None else 0
    # Mirror only the balance movement as a 0.4.1-supported calibration voucher.
    repo = WalletRepository(db)
    base = repo.totals(vendor)
    movement = r - c + a
    if movement:
        repo.calibrate(
            work,
            vendor,
            "0",
            target_pico=base.b + movement,
            expected_revision=base.revision,
            at=at,
            operation_id=operation + ":local-balance",
            record_operation=False,
        )
    if r or c or a:
        post(
            db,
            work,
            vendor,
            at,
            operation,
            r,
            c,
            a - movement,
            f"本地累计校准:充值 {_usd(before['R'])}→{_usd(targets['R'])}; "
            f"消费 {_usd(before['C'])}→{_usd(targets['C'])}; "
            f"余额 {_usd(before['B'])}→{_usd(int(before['B']) + movement)}",
        )
    if any(str(targets[key]) != before[key] for key in ("pastTopUpPico", "pastConsumptionPico")):
        db.execute(
            "INSERT INTO local_wallet_metadata VALUES(?,?,?) ON CONFLICT(vendor_id) DO UPDATE SET "
            "past_r=excluded.past_r,past_c=excluded.past_c",
            (vendor, str(targets["pastTopUpPico"]), str(targets["pastConsumptionPico"])),
        )
        work.touch("wallet")


def day_end(day: str, zone: str) -> str:
    from htu.adapters.sqlite.wallet_closing import bounds

    return format_instant(Instant(bounds(day, zone)[1].value - timedelta(minutes=1)))


def entries(db: sqlite3.Connection, vendor: str, cursor: str | None, limit: int) -> dict[str, Any]:
    from htu.adapters.sqlite.wallet_inheritance import entries as inherited_entries
    from htu.adapters.sqlite.wallet_inheritance import seed

    if seed(db, vendor):
        return inherited_entries(db, vendor, cursor, limit, "local")
    db.create_function("htu_local_day_end", 2, day_end, deterministic=True)
    edge = ["9999", "~"]
    if cursor is not None:
        try:
            parsed: object = json.loads(cursor)
            if (
                not isinstance(parsed, list)
                or len(cast(list[object], parsed)) != 2
                or not all(isinstance(v, str) for v in cast(list[object], parsed))
            ):
                raise ValueError
            edge = cast(list[str], parsed)
        except (ValueError, TypeError) as error:
            raise DomainError("ACCOUNT_CURSOR_INVALID", "流水游标无效") from error
    rows = db.execute(
        "SELECT * FROM (SELECT 'v:'||id id,occurred_at,kind,"
        "CASE WHEN kind='top_up' THEN amount_pico ELSE '0' END r,'0' c,"
        "CASE WHEN kind='adjustment' THEN amount_pico ELSE '0' END a,note FROM wallet_entry "
        "WHERE vendor_id=? AND kind IN ('top_up','adjustment') UNION ALL "
        "SELECT 'l:'||id,occurred_at,'adjustment',r,c,a,note FROM local_wallet_posting WHERE vendor_id=? "
        "UNION ALL SELECT 'd:'||day||':'||timezone,htu_local_day_end(day,timezone),'consumption',"
        "'0',original,'0','本地已结业务日估算' FROM wallet_daily_close WHERE vendor_id=? UNION ALL "
        "SELECT 'c:'||id,posted_at,'adjustment','0',htu_pico_sub(after_pico,before_pico),'0',"
        "'历史重算校准 '||day FROM wallet_daily_correction WHERE vendor_id=?) "
        "WHERE (occurred_at,id)<(?,?) ORDER BY occurred_at DESC,id DESC LIMIT ?",
        (vendor, vendor, vendor, vendor, *edge, limit + 1),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows[:limit]:
        used = sum(
            max(abs(int(p[k])) for k in ("r", "c", "a"))
            for p in db.execute(
                "SELECT r,c,a FROM local_wallet_posting WHERE vendor_id=? AND original_id=?",
                (vendor, row["id"]),
            )
        )
        available = (
            max(0, max(abs(int(row[k])) for k in ("r", "c", "a")) - used) if row["id"].startswith("v:") else 0
        )
        result.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "occurredAt": row["occurred_at"],
                "R": row["r"],
                "C": row["c"],
                "A": row["a"],
                "note": row["note"],
                "originalId": None,
                "intervalId": None,
                "availablePico": str(available),
            }
        )
    return {
        "entries": result,
        "nextCursor": json.dumps([rows[limit - 1]["occurred_at"], rows[limit - 1]["id"]])
        if len(rows) > limit
        else None,
        "revision": db.execute("SELECT value FROM revision WHERE scope='wallet'").fetchone()[0],
    }


def reclassify(
    db: sqlite3.Connection,
    work: UnitOfWork,
    vendor: str,
    values: dict[str, object],
    operation: str,
    at: Instant,
) -> None:
    require_active(WalletRepository(db).vendor(vendor))
    if values["expectedAccountRevision"] != work.revision("wallet"):
        raise DomainError("WALLET_REVISION_CONFLICT", "本地流水已变化，请重新读取")
    identity = str(values["entryId"])
    row = db.execute(
        "SELECT *,CASE WHEN (CASE WHEN r<>'0' THEN r WHEN c<>'0' THEN c ELSE a END) LIKE '-%' "
        "THEN '-'||available ELSE available END amount_pico "
        "FROM wallet_inherited_entry WHERE vendor_id=? AND id=? AND available<>'0'",
        (vendor, identity),
    ).fetchone()
    inherited_row = row is not None
    if row is None and identity.startswith("v:"):
        row = db.execute(
            "SELECT * FROM wallet_entry WHERE id=? AND vendor_id=? AND kind IN ('top_up','adjustment')",
            (identity.removeprefix("v:"), vendor),
        ).fetchone()
    if row is None:
        raise DomainError("ACCOUNT_ENTRY_INVALID", "原本地流水不可改判")
    from htu.adapters.sqlite.wallet_command_store import account_amount

    amount = account_amount(values, "amount")
    used = sum(
        max(abs(int(p[k])) for k in ("r", "c", "a"))
        for p in db.execute(
            "SELECT r,c,a FROM local_wallet_posting WHERE vendor_id=? AND original_id=? "
            "AND id>COALESCE((SELECT json_extract(payload,'$.local_seq') "
            "FROM wallet_inheritance WHERE vendor_id=?),0)",
            (vendor, identity, vendor),
        )
    )
    if amount <= 0 or amount + used > abs(int(row["amount_pico"])):
        raise DomainError("ACCOUNT_AMOUNT_INVALID", "改判金额超过原记录剩余金额")
    source = (
        ("r" if int(row["r"]) else "c" if int(row["c"]) else "a")
        if inherited_row
        else ("r" if row["kind"] == "top_up" else "a")
    )
    target = {"top_up": "r", "consumption": "c", "adjustment": "a"}[str(values["targetKind"])]
    delta: dict[str, int] = dict.fromkeys(("r", "c", "a"), 0)
    signed = amount if int(row["amount_pico"]) >= 0 else -amount
    delta[source] -= signed
    # Consumption has the opposite balance contribution to recharge/adjustment.
    delta[target] += -signed if (target == "c") != (source == "c") else signed
    after = summary(db, vendor)
    if int(after["R"]) + delta["r"] < 0 or int(after["C"]) + delta["c"] < 0:
        raise DomainError("ACCOUNT_AMOUNT_INVALID", "改判后的累计金额不能为负数")
    if source == target:
        raise DomainError("ACCOUNT_ENTRY_INVALID", "请选择不同的事务类型")
    post(
        db,
        work,
        vendor,
        at,
        operation,
        delta["r"],
        delta["c"],
        delta["a"],
        str(values.get("note", "本地流水类型更正")),
        identity,
    )
