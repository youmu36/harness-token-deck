"""Reproducible isolated before/after workload; --root can point to a source snapshot."""

import argparse
import hashlib
import json
import math
import os
import sqlite3
import statistics
import sys
import tempfile
import tracemalloc
from collections import Counter
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from time import perf_counter
from unittest.mock import patch


def pages(path: Path) -> dict[str, int]:
    """Count owned B-tree/overflow pages when SQLite was built without dbstat."""
    data = path.read_bytes()
    size = int.from_bytes(data[16:18], "big") or 65536
    usable = size - data[20]
    with sqlite3.connect(path) as db:
        roots = [
            ("sqlite_schema", 1),
            *db.execute("SELECT name,rootpage FROM sqlite_schema WHERE rootpage>0"),
        ]

    def varint(offset: int) -> tuple[int, int]:
        value = 0
        for i in range(9):
            byte = data[offset + i]
            if i == 8:
                return (value << 8) | byte, i + 1
            value = (value << 7) | (byte & 127)
            if byte < 128:
                return value, i + 1
        raise ValueError("Invalid SQLite varint")

    result = {}
    for name, root in roots:
        todo, seen = [root], set()
        while todo:
            number = todo.pop()
            if number in seen:
                continue
            seen.add(number)
            base = (number - 1) * size
            header = base + (100 if number == 1 else 0)
            kind = data[header]
            interior = kind in (2, 5)
            if interior:
                todo.append(int.from_bytes(data[header + 8 : header + 12], "big"))
            count = int.from_bytes(data[header + 3 : header + 5], "big")
            pointer = header + (12 if interior else 8)
            for i in range(count):
                cell = base + int.from_bytes(data[pointer + 2 * i : pointer + 2 * i + 2], "big")
                if interior:
                    todo.append(int.from_bytes(data[cell : cell + 4], "big"))
                    cell += 4
                if kind == 5:
                    continue
                length, used = varint(cell)
                cell += used
                if kind == 13:
                    _, used = varint(cell)
                    cell += used
                maximum = usable - 35 if kind == 13 else (usable - 12) * 64 // 255 - 23
                if length <= maximum:
                    continue
                minimum = (usable - 12) * 32 // 255 - 23
                local = minimum + (length - minimum) % (usable - 4)
                if local > maximum:
                    local = minimum
                overflow = int.from_bytes(data[cell + local : cell + local + 4], "big")
                while overflow:
                    if overflow in seen:
                        raise ValueError("Duplicate SQLite overflow page")
                    seen.add(overflow)
                    offset = (overflow - 1) * size
                    overflow = int.from_bytes(data[offset : offset + 4], "big")
        result[name] = len(seen) * size
    if sum(result.values()) != len(data):
        raise ValueError("B-tree ownership did not account for the compacted file")
    return result


def key_layout() -> list[dict[str, object]]:
    report = []
    for retained in (500, 10000, 30000):
        for mode in ("hex-text", "blob-key", "integer-id"):
            with sqlite3.connect(":memory:") as db:
                primary = "PRIMARY KEY(harness,key)" if mode != "integer-id" else "UNIQUE(harness,key)"
                identity_id = "usage_id INTEGER PRIMARY KEY," if mode == "integer-id" else ""
                kind = "TEXT" if mode == "hex-text" else "BLOB"
                db.execute(
                    f"CREATE TABLE identity({identity_id}harness TEXT,key {kind},occurred TEXT,"
                    f"billed INTEGER,seen TEXT,origin TEXT,{primary})"
                    + (" WITHOUT ROWID" if mode != "integer-id" else "")
                )
                identity = (
                    "usage_id INTEGER PRIMARY KEY," if mode == "integer-id" else f"harness TEXT,key {kind},"
                )
                db.execute(
                    "CREATE TABLE ledger("
                    + identity
                    + "occurred TEXT,session TEXT,model TEXT,grp TEXT,amount TEXT,body TEXT"
                    + (",PRIMARY KEY(harness,key)) WITHOUT ROWID" if mode != "integer-id" else ")")
                )
                for name, columns in (
                    ("time", "occurred"),
                    ("session", "session,occurred"),
                    ("group", "grp,occurred"),
                    ("statistics", "session,occurred"),
                    ("model", "model,occurred"),
                    ("unpriced", "occurred,model"),
                ):
                    db.execute(f"CREATE INDEX idx_{name} ON ledger({columns})")
                began = perf_counter()
                for i in range(30000):
                    key = hashlib.sha256(str(i).encode()).digest()
                    key = key.hex() if mode == "hex-text" else key
                    at = f"2026-09-{i % 28 + 1:02}T00:00:00.000Z"
                    db.execute(
                        "INSERT INTO identity VALUES(" + ("NULL," if identity_id else "") + "?,?,?,?,?,?)",
                        ("codex", key, at, 1, at, "live"),
                    )
                    if i < retained:
                        values = ((i + 1,) if identity_id else ("codex", key)) + (
                            at,
                            "session-" + str(i // 10),
                            "model-" + str(i % 10),
                            "group-a",
                            str(10**20 + i),
                            "x" * 110,
                        )
                        db.execute("INSERT INTO ledger VALUES(" + ",".join("?" for _ in values) + ")", values)
                db.commit()
                elapsed = (perf_counter() - began) * 1000
                db.execute("VACUUM")
                report.append(
                    {
                        "mode": mode,
                        "identities": 30000,
                        "retained": retained,
                        "bytes": db.execute("PRAGMA page_count").fetchone()[0] * 4096,
                        "writeMs": round(elapsed, 3),
                    }
                )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", default="current")
    parser.add_argument("--key-layout", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path[:0] = [str(root / "backend/src"), str(root)]
    if args.key_layout:
        args.output.write_text(json.dumps(key_layout(), indent=2), encoding="utf-8")
        return
    from htu.adapters.sqlite import ledger_read, usage_archive, usage_hours
    from htu.adapters.sqlite.wallet_repo import WalletRepository
    from htu.bootstrap import Application
    from htu.domain.common.testing import FakeClock
    from htu.domain.common.time import Instant, format_instant
    from htu.domain.usage.model import Harness
    from htu.domain.wallet.model import UsageRef, WalletEntry

    from scripts.measure_feedback_queries import END, START, seed

    report: dict[str, object] = {
        "label": args.label,
        "fixture": {
            "identities": 30000,
            "details": 12000,
            "activeMinutes": 45000,
            "archiveMinutes": 45000,
            "walletMinutes": 100000,
            "auditItems": 478,
        },
        "queries": [],
        "results": {},
        "cacheDefinition": "Application cache cold/warm; cold also releases idle SQLite readers. "
        "OS file cache is not flushed.",
    }
    with tempfile.TemporaryDirectory(prefix="htu-storage-measure-") as temporary:
        base = Path(temporary)
        for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "CODEX_HOME"):
            value = base / name
            value.mkdir()
            os.environ[name] = str(value)
        for name in ("CODEX", "CLAUDE", "ZCODE", "DSH"):
            os.environ["HTU_SOURCE_ROOT_" + name] = str(base / ("source-" + name))
        writes = []
        for repetition in range(3):
            with Application(
                base / f"write-{repetition}", clock=FakeClock(END), start_scheduler=False
            ) as writing:
                writing.writer.execute(
                    lambda w: WalletRepository(w.connection).create_vendor(
                        w,
                        vendor_id="write",
                        name="Write",
                        initial_balance="10000",
                        at=END,
                        operation_id="create",
                    )
                )

                def append(work, begin: int, count: int):
                    repo = WalletRepository(work.connection)
                    for i in range(begin, begin + count):
                        at = Instant(END.value + timedelta(seconds=i + 1))
                        repo.append_with_offsets(
                            work,
                            WalletEntry(
                                str(i),
                                "write",
                                "consumption",
                                10**10 + i,
                                at,
                                repo.next_seq(),
                                usage_ref=UsageRef(Harness.CODEX, f"{i:064x}"),
                            ),
                            at=at,
                        )

                writing.writer.execute(lambda w: append(w, 0, 3000))
                began = perf_counter()
                writing.writer.execute(lambda w: append(w, 3000, 500))
                writes.append((perf_counter() - began) * 1000)
                total = writing.writer.execute(lambda w: WalletRepository(w.connection).totals("write"))
                if total.c != sum(10**10 + i for i in range(3500)):
                    raise AssertionError("Wallet write benchmark changed exact money")
        report["walletWrite500"] = {
            "existingRequests": 3000,
            "p50Ms": round(statistics.median(writes), 3),
            "p95Ms": round(max(writes), 3),
        }
        with Application(base / "data", clock=FakeClock(END), start_scheduler=False) as app:
            began = perf_counter()
            seed(app)
            report["seedMs"] = round((perf_counter() - began) * 1000, 3)

            def evidence(work):
                db = work.connection
                modern = any(row[1] == "encoding" for row in db.execute("PRAGMA table_info(usage_archive)"))
                db.executemany(
                    "INSERT INTO identity VALUES('codex',?,?,1,?,'live')",
                    (
                        (
                            hashlib.sha256(str(i).encode()).digest()
                            if modern
                            else hashlib.sha256(str(i).encode()).hexdigest(),
                            format_instant(START),
                            format_instant(END),
                        )
                        for i in range(30000)
                    ),
                )
                for batch in range(2):
                    identifier = f"audit-{batch}"
                    db.execute(
                        "INSERT INTO recalc_batch(id,harness,start_at,end_at,group_id,"
                        "version_id,record_count,old_pico,new_pico,unpriced_count,"
                        "incomplete_count,fingerprint,committed_at,operation_id) VALUES(?,"
                        "'codex',?,?,'group-id-11111111-1111-1111-1111-11111111',"
                        "'version-id-11111111-1111-1111-11111111',239,'239','478',0,0,?,?,?)",
                        (
                            identifier,
                            format_instant(START),
                            format_instant(END),
                            "a" * 64,
                            format_instant(END),
                            identifier,
                        ),
                    )
                    for i in range(239):
                        old = {
                            "price_group_id": "group-id-11111111-1111-1111-1111-11111111",
                            "price_version_id": "version-id-11111111-1111-1111-11111111",
                            "price_revision_id": "revision-11111111-1111-1111-11111111",
                            "pico": str(10**20 + i),
                            "incomplete": 0,
                            "unpriced": 0,
                            "display_name": "Model",
                            "rule_applied": "base",
                            "billed_at": format_instant(START),
                        }
                        new = old | {"pico": str(10**20 + i + 1), "billed_at": format_instant(END)}
                        key = bytes.fromhex(f"{i:064x}") if modern else f"{i:064x}"
                        db.execute(
                            "INSERT INTO recalc_item(batch_id,harness,dedupe_key,old_json,"
                            "new_json) VALUES(?,'codex',?,?,?)",
                            (
                                identifier,
                                key,
                                json.dumps(old, separators=(",", ":")),
                                json.dumps(new, separators=(",", ":")),
                            ),
                        )
                return modern

            modern = app.writer.execute(evidence)

            began = perf_counter()

            def maintain(work):
                while usage_hours.refresh_hours(work.connection):
                    pass
                while usage_archive.optimize_archives(work.connection, limit=20):
                    pass
                if modern:
                    from htu.adapters.sqlite.recalc_codec import compact_audits

                    while compact_audits(work.connection):
                        pass

            app.writer.execute(maintain)
            report["projectionAndAuditMs"] = round((perf_counter() - began) * 1000, 3)
            if modern:
                from htu.adapters.sqlite.wallet_archive import compact
                from htu.adapters.sqlite.wallet_days import build

                began = perf_counter()
                while app.writer.execute(
                    lambda w: compact(w.connection, at=Instant(END.value + timedelta(days=90)))
                ):
                    pass
                report["coldFinanceMs"] = round((perf_counter() - began) * 1000, 3)
                began = perf_counter()
                while not app.writer.execute(lambda w: build(w.connection, 345)):
                    pass
                report["newOffsetDirectoryBuildMs"] = round((perf_counter() - began) * 1000, 3)
            app.readers.release_idle()

            def compact_file(work):
                work.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                work.connection.execute("VACUUM")
                work.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            app.writer.submit_idle(compact_file).result()
            report["pageBytes"] = pages(app.writer.path)
            report["mainFileBytes"] = app.writer.path.stat().st_size

            scenarios = [
                ("today-source", END.value - timedelta(days=1), END.value, "codex", None, 480, "hour"),
                ("24-hours-all", END.value - timedelta(days=1), END.value, "all", None, 480, "hour"),
                ("7-days-all", END.value - timedelta(days=7), END.value, "all", None, 480, "day"),
                ("30-days-all", START.value, END.value, "all", None, 480, "day"),
                (
                    "model-hours",
                    START.value + timedelta(days=9),
                    START.value + timedelta(days=10),
                    "codex",
                    "model-3",
                    480,
                    "hour",
                ),
                (
                    "model-minutes",
                    START.value + timedelta(days=9),
                    START.value + timedelta(days=10),
                    "codex",
                    "model-3",
                    480,
                    "minute",
                ),
                ("fractional-offset", START.value, END.value, "all", None, 345, "day"),
            ]
            statements: set[str] = set()
            original_open = __import__(
                "htu.adapters.sqlite.connection", fromlist=["open_connection"]
            ).open_connection

            def trace(sql):
                if sql.lstrip().upper().startswith("SELECT") and any(
                    "FROM " + name in sql
                    for name in ("ledger", "usage_archive", "usage_hour_model", "usage_rollup", "wallet_day")
                ):
                    statements.add(sql)

            def opened(*values, **keywords):
                db = original_open(*values, **keywords)
                db.set_trace_callback(trace)
                return db

            raw_details, raw_rollups = ledger_read.LedgerReader._details, ledger_read.LedgerReader._rollups
            raw_archived, raw_unpack = usage_archive.archived_rows, usage_archive.unpack
            for name, begin, end, source, model, offset, grain in scenarios:
                payload = {
                    "harness": source,
                    "start": format_instant(Instant(begin)),
                    "end": format_instant(Instant(end)),
                    "offsetMinutes": offset,
                    "grain": grain,
                    "modelKey": model,
                }
                for cache in ("cold", "warm"):
                    times, counts = [], Counter()

                    def details(self, *values, counts=counts, **keywords):
                        for row in raw_details(self, *values, **keywords):
                            counts["detailRows"] += 1
                            yield row

                    def rollups(self, *values, counts=counts, **keywords):
                        for row in raw_rollups(self, *values, **keywords):
                            counts["projectionRows"] += 1
                            yield row

                    def archived(*values, counts=counts, **keywords):
                        for row in raw_archived(*values, **keywords):
                            counts["archiveRows"] += 1
                            yield row

                    def unpack(blob, counts=counts):
                        rows = raw_unpack(blob)
                        counts["minuteBlocksDecoded"] += 1
                        counts["minuteRowsDecoded"] += len(rows)
                        counts["compressedMinuteBytes"] += len(blob)
                        return rows

                    with ExitStack() as stack:
                        stack.enter_context(patch("htu.adapters.sqlite.connection.open_connection", opened))
                        stack.enter_context(patch.object(ledger_read.LedgerReader, "_details", details))
                        stack.enter_context(patch.object(ledger_read.LedgerReader, "_rollups", rollups))
                        stack.enter_context(patch.object(ledger_read, "archived_rows", archived))
                        stack.enter_context(patch.object(usage_archive, "unpack", unpack))
                        for _ in range(3):
                            if cache == "cold":
                                app.statistics.invalidate()
                                app.readers.release_idle()
                            began = perf_counter()
                            response = app.router.call("stats.summary", payload)
                            times.append((perf_counter() - began) * 1000)
                            if not response["ok"]:
                                raise AssertionError(response)
                            result = response["data"] | {"revision": 0}
                            previous = report["results"].setdefault(name, result)
                            if result != previous:
                                raise AssertionError("Unstable query: " + name)
                    report["queries"].append(
                        {
                            "name": name,
                            "cache": cache,
                            "p50Ms": round(statistics.median(times), 3),
                            "p95Ms": round(sorted(times)[math.ceil(len(times) * 0.95) - 1], 3),
                            **{key: value / 3 for key, value in counts.items()},
                        }
                    )
            wallet_results = {}
            for offset in (0, 345):
                times = []
                for _ in range(3):
                    app.wallet_store.release_cache()
                    began = perf_counter()
                    cursor, combined = None, []
                    for _ in range(4):
                        page = app.wallet_store.wallet_entries("perf", cursor, 20, offset_minutes=offset)
                        combined.extend(page["entries"])
                        cursor = page["nextCursor"]
                        if cursor is None:
                            break
                    times.append((perf_counter() - began) * 1000)
                    wallet_results[str(offset)] = combined
                report["queries"].append(
                    {
                        "name": "wallet-pages-" + str(offset),
                        "cache": "cold",
                        "p50Ms": round(statistics.median(times), 3),
                        "p95Ms": round(max(times), 3),
                    }
                )
            report["results"]["wallet"] = wallet_results
            # One bounded memory sample; it is separate from the latency repetitions.
            app.statistics.invalidate()
            tracemalloc.start()
            app.router.call(
                "stats.summary",
                {
                    "harness": "all",
                    "start": format_instant(START),
                    "end": format_instant(END),
                    "offsetMinutes": 480,
                    "grain": "day",
                },
            )
            report["queryPeakPythonBytes"] = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            with app.readers.connection() as db:
                plans = []
                for sql in sorted(statements):
                    if (
                        "FROM usage_archive" in sql
                        or "FROM usage_hour_model" in sql
                        or ("FROM ledger" in sql and "WHERE occurred_at" in sql)
                    ):
                        plans.append(
                            {"sql": sql, "plan": [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql)]}
                        )
                report["queryPlans"] = plans
                report["integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0]
                report["foreignKeyErrors"] = len(db.execute("PRAGMA foreign_key_check").fetchall())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key not in ("results", "queryPlans", "pageBytes")
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
