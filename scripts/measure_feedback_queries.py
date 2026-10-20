"""Comparable isolated query scenarios for the current Feedback, never real user data."""

import argparse
import json
import tempfile
import tracemalloc
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from threading import Event, Lock
from time import perf_counter
from typing import Any
from unittest.mock import patch

import psutil
from htu.adapters.sqlite import ledger_read, usage_archive
from htu.adapters.sqlite.connection import open_connection
from htu.adapters.sqlite.statistics_store import SqliteStatisticsSnapshot
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.bootstrap import Application
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import Instant, format_instant, parse_instant
from htu.domain.statistics.aggregator import Aggregator
from htu.domain.usage.model import Harness

from backend.tests.contracts.command_helpers import seed_rows
from backend.tests.unit.statistics.helpers import row

START = parse_instant("2026-09-01T00:00:00Z")
END = parse_instant("2026-10-01T00:00:00Z")


class TimedLock:
    def __init__(self, lock: Any) -> None:
        self.lock = lock
        self.wait_ms = 0.0

    def __enter__(self) -> None:
        began = perf_counter()
        self.lock.acquire()
        self.wait_ms += (perf_counter() - began) * 1000

    def __exit__(self, *args: Any) -> None:
        self.lock.release()


def concurrency(app: Application, payload: dict[str, Any], *, serialized: bool = False) -> dict[str, float]:
    app.statistics.invalidate()
    entered = Event()
    original = ledger_read.LedgerReader.iter_rows
    lock = TimedLock(app.statistics._lock)
    gate = TimedLock(Lock())
    summary = app.statistics.summary

    def reading(self: Any, *args: Any, **kwargs: Any):
        entered.set()
        yield from original(self, *args, **kwargs)

    def serial_summary(*args: Any, **kwargs: Any):
        with gate:
            return summary(*args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch.object(app.statistics, "_lock", lock))
        stack.enter_context(patch.object(ledger_read.LedgerReader, "iter_rows", reading))
        if serialized:
            stack.enter_context(patch.object(app.statistics, "summary", serial_summary))
        with ThreadPoolExecutor(2) as executor:
            began = perf_counter()
            history = executor.submit(app.router.call, "stats.summary", payload)
            assert entered.wait(5)
            monitor_began = perf_counter()
            monitor = executor.submit(app.statistics.cache_monitor, Harness.CODEX, "hour", 345)
            monitor.result(30)
            monitor_ms = (perf_counter() - monitor_began) * 1000
            assert history.result(30)["ok"]
            return {
                "monitorResponseMs": monitor_ms,
                "totalMs": (perf_counter() - began) * 1000,
                "cacheLockWaitMs": lock.wait_ms,
                "serializationWaitMs": gate.wait_ms,
            }


def seed(app: Application) -> None:
    seed_rows(
        app,
        (
            row(
                i + 1,
                i % 60,
                hour=(i // 60) % 24,
                day=28 + i // 4320,
                model=f"model-{i % 10}",
                pico=10**20 + i,
            )
            for i in range(12_000)
        ),
    )

    def history(work):
        db = work.connection
        columns = [r[1] for r in db.execute("PRAGMA table_info(usage_rollup)")]
        blocks = []
        for index in range(90_000):
            at = Instant(START.value + timedelta(minutes=index // 3))
            model = f"model-{index % 10}"
            record = dict.fromkeys(columns, 0)
            record.update(
                harness="codex",
                bucket=format_instant(at),
                vendor_id="",
                model_key=model,
                model_id=model,
                display_name=model,
                records=3,
                input=300,
                cache_read=120,
                output=30,
                rated_input=300,
                rated_cache=120,
                pico=str(10**20 + index),
            )
            if index < 45_000:
                blocks.append(record)
                if len(blocks) == 500:
                    usage_archive._insert(db, blocks)
                    blocks = []
            else:
                db.execute(
                    "INSERT INTO usage_rollup VALUES(" + ",".join("?" for _ in columns) + ")",
                    tuple(record[c] for c in columns),
                )
        for day in range(1, 22):
            db.execute("INSERT OR IGNORE INTO usage_day_revision VALUES('codex',?,1)", (f"2026-09-{day:02}",))
        WalletRepository(db).create_vendor(work, vendor_id="perf", name="perf", at=START, operation_id="perf")
        db.execute("UPDATE wallet_total SET last_seq=100000 WHERE vendor_id='perf'")
        db.executemany(
            "INSERT INTO wallet_activity VALUES(?, 'perf', ?, 'consumption', ?, 1, '', NULL, NULL)",
            (
                (i + 1, format_instant(Instant(START.value + timedelta(minutes=i))), str(10**20 + i))
                for i in range(100_000)
            ),
        )
        work.touch("usage")
        work.touch("wallet")

    app.writer.execute(history)


def measurements(app: Application):
    counts = Counter()
    original_open = open_connection
    original_snapshot = SqliteStatisticsSnapshot.__init__
    original_unpack = usage_archive.unpack
    original_push = Aggregator.push

    def trace(statement):
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            counts["queries"] += 1

    def configure(db):
        db.set_trace_callback(trace)
        db.set_progress_handler(lambda: counts.update(sqliteVmSteps=1000) or 0, 1000)
        return db

    def opened(*args, **kwargs):
        return configure(original_open(*args, **kwargs))

    def snapshot(self, db):
        configure(db)
        original_snapshot(self, db)

    def unpack(payload):
        records = original_unpack(payload)
        counts["archiveBlocks"] += 1
        counts["archiveMinutesDecoded"] += len(records)
        return records

    def push(self, record):
        counts["aggregationRows"] += 1
        began = perf_counter()
        try:
            return original_push(self, record)
        finally:
            counts["aggregationMicroseconds"] += (perf_counter() - began) * 1_000_000

    stack = ExitStack()
    stack.enter_context(patch("htu.adapters.sqlite.connection.open_connection", opened))
    stack.enter_context(patch.object(SqliteStatisticsSnapshot, "__init__", snapshot))
    stack.enter_context(patch.object(usage_archive, "unpack", unpack))
    stack.enter_context(patch.object(Aggregator, "push", push))
    return stack, counts


def run(label: str, output: Path, *, concurrency_only: bool = False) -> None:
    with tempfile.TemporaryDirectory(prefix="htu-feedback-") as directory:  # noqa: SIM117
        with Application(Path(directory), clock=FakeClock(END), start_scheduler=False) as app:
            seed(app)
            app.readers.release_idle()
            report = {
                "label": label,
                "fixture": {
                    "details": 12000,
                    "minuteRows": 45000,
                    "archivedMinutes": 45000,
                    "walletMinutes": 100000,
                },
                "scenarios": [],
            }
            if concurrency_only:
                report = json.loads(output.read_text(encoding="utf-8"))
                base = {
                    "harness": "codex",
                    "start": format_instant(START),
                    "end": format_instant(END),
                    "offsetMinutes": 345,
                    "grain": "day",
                    "sections": ["trend", "models"],
                }
                report["concurrent"] = concurrency(app, base)
                report["serializedControl"] = concurrency(app, base, serialized=True)
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({key: report[key] for key in ("concurrent", "serializedControl")}, indent=2))
                return
            stack, counts = measurements(app)
            with stack:

                def measure(name, command, payload):
                    counts.clear()
                    rss = psutil.Process().memory_info().rss
                    tracemalloc.start()
                    began = perf_counter()
                    result = app.router.call(command, payload)
                    elapsed = (perf_counter() - began) * 1000
                    _, peak = tracemalloc.get_traced_memory()
                    tracemalloc.stop()
                    assert result["ok"], result
                    report["scenarios"].append(
                        {
                            "name": name,
                            **dict(counts),
                            "responseMs": elapsed,
                            "responseBytes": len(json.dumps(result, ensure_ascii=False).encode()),
                            "peakPythonBytes": peak,
                            "rssDeltaBytes": psutil.Process().memory_info().rss - rss,
                        }
                    )

                base = {
                    "harness": "codex",
                    "start": format_instant(START),
                    "end": format_instant(END),
                    "offsetMinutes": 345,
                    "grain": "day",
                    "sections": ["trend", "models"],
                }
                for name, payload in (
                    ("history-cold", base),
                    ("history-hit", base),
                    ("seven-days", base | {"start": "2026-09-24T00:00:00.000Z"}),
                    ("eight-days", base | {"start": "2026-09-23T00:00:00.000Z"}),
                    (
                        "hours-open",
                        base
                        | {
                            "start": "2026-09-10T00:00:00.000Z",
                            "end": "2026-09-11T00:00:00.000Z",
                            "grain": "hour",
                            "sections": ["hourlyTotals"],
                        },
                    ),
                    (
                        "model-hours",
                        base
                        | {
                            "start": "2026-09-10T00:00:00.000Z",
                            "end": "2026-09-11T00:00:00.000Z",
                            "grain": "hour",
                            "sections": ["hourly"],
                            "modelKey": "model-1",
                        },
                    ),
                    ("recent-monitor", {"harness": "codex", "mode": "hour", "offsetMinutes": 345}),
                ):
                    measure(
                        name, "stats.cacheMonitor" if name == "recent-monitor" else "stats.summary", payload
                    )
                wallet = {"vendorId": "perf", "limit": 20, "offsetMinutes": 345}
                measure("wallet-cold", "wallet.entries", wallet)
                measure("wallet-hit", "wallet.entries", wallet)
                cursor = app.wallet_store.wallet_entries("perf", None, 20, offset_minutes=345)["nextCursor"]
                measure("wallet-next-page", "wallet.entries", wallet | {"cursor": cursor})
            # Use a real historical read concurrently with the monitor, without tracing overhead.
            app.statistics.invalidate()
            entered = Event()
            original = ledger_read.LedgerReader.iter_rows

            def reading(self, *args, **kwargs):
                entered.set()
                yield from original(self, *args, **kwargs)

            with (
                patch.object(ledger_read.LedgerReader, "iter_rows", reading),
                ThreadPoolExecutor(2) as executor,
            ):
                began = perf_counter()
                history = executor.submit(app.router.call, "stats.summary", base)
                assert entered.wait(5)
                monitor_began = perf_counter()
                monitor = executor.submit(
                    app.statistics.cache_monitor,
                    Harness.CODEX,
                    "hour",
                    345,
                )
                monitor.result(30)
                monitor_ms = (perf_counter() - monitor_began) * 1000
                history.result(30)
                report["concurrent"] = {
                    "monitorResponseMs": monitor_ms,
                    "totalMs": (perf_counter() - began) * 1000,
                }
            report["resources"] = {
                "statistics": app.statistics.diagnostics(),
                "readPool": app.readers.diagnostics(),
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--concurrency-only", action="store_true")
    args = parser.parse_args()
    run(args.label, args.out, concurrency_only=args.concurrency_only)
