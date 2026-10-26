"""Synthetic transaction/cache CPU costs; complements native process measurements."""

import argparse
import inspect
import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.projection_snapshot import ProjectionSnapshot
from htd.adapters.sqlite.writer import WriteExecutor

from backend.tests.integration.collection_helpers import AT, collect, quote, usage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--without-monitor-triggers", action="store_true")
    args = parser.parse_args()
    root = Path.cwd()
    report: dict[str, Any] = {
        "label": args.label,
        "withoutMonitorTriggers": args.without_monitor_triggers,
        "transactions": [],
        "cache": {},
    }
    for sessions in (1, 8):
        for repeat in range(3):
            path = root / ".verify/cpu-costs" / (args.label + "-" + uuid4().hex) / "htd.sqlite3"
            with WriteExecutor(path) as writer:
                if args.without_monitor_triggers:

                    def disable(work: Any) -> None:
                        for row in work.connection.execute(
                            "SELECT name FROM sqlite_schema WHERE type='trigger' AND name GLOB 'monitor_*'"
                        ).fetchall():
                            work.connection.execute('DROP TRIGGER "' + row[0] + '"')

                    writer.execute(disable)
                # Initialization and input construction are outside the measured interval.
                collect(writer, (usage(1),), at=AT)
                batches = [
                    tuple(
                        usage(2 + batch * 128 + index, session=f"s-{index % sessions}")
                        for index in range(128)
                    )
                    for batch in range(12)
                ]
                before = time.process_time()
                started = time.perf_counter()
                for batch, events in enumerate(batches):
                    collect(writer, events, at=AT, offset=batch + 2)
                    writer.execute(
                        lambda work: IngestRepository(work.connection).bill_batch(work, quote, at=AT)
                    )
                report["transactions"].append(
                    {
                        "sessions": sessions,
                        "repeat": repeat,
                        "cpuSeconds": time.process_time() - before,
                        "wallSeconds": time.perf_counter() - started,
                    }
                )
    monitor = {
        "trend": [
            {
                "bucket": str(i),
                "inputTokens": 100,
                "outputTokens": 10,
                "cacheHitRate": 0.4,
                "cacheReadTokens": 40,
                "cacheBust": False,
                "cacheLoss": False,
                "contextCompacted": False,
            }
            for i in range(60)
        ],
        "sessionHitRates": [{"sessionRef": "x" * 64, "frozen": False}],
        "revision": 1,
    }
    for name, payload, loops in (
        ("monitor", monitor, 20000),
        (
            "analysis",
            {
                "totalModels": 1024,
                "models": [{"modelKey": f"model-{i}", "picoUsd": str(10**24 + i)} for i in range(1024)],
                "quality": [],
            },
            2000,
        ),
    ):
        before = time.process_time()
        options = (
            {"compress": False}
            if name == "monitor" and "compress" in inspect.signature(ProjectionSnapshot.build).parameters
            else {}
        )
        snapshot = ProjectionSnapshot.build(payload, **options)
        for _ in range(loops):
            snapshot = ProjectionSnapshot.build(payload, **options)
        build_cpu = time.process_time() - before
        before = time.process_time()
        for _ in range(loops):
            snapshot.materialize({} if name == "analysis" else None)
        report["cache"][name] = {
            "loops": loops,
            "buildCpuSeconds": build_cpu,
            "hitCpuSeconds": time.process_time() - before,
            "retainedBytes": snapshot.retained_bytes,
        }
    destination = root / f".verify/cpu-costs-{args.label}.json"
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
