"""Equal-input host collection and paged-history reads against isolated source snapshots."""

import argparse
import json
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from htd.domain.common.time import Instant, format_instant

from backend.tests.integration.scan_helpers import headers, request
from scripts.probes.data_refactor_runtime import measure
from scripts.probes.ui_probe import ROOT, UiProbe


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--requests", type=int, default=160)
    parser.add_argument("--seconds", type=float, default=6)
    parser.add_argument("--history-models", type=int, default=1024)
    parser.add_argument("--batch-size", type=int, default=0)
    args = parser.parse_args()
    if not 1 <= args.requests <= 2000 or args.seconds < 2:
        parser.error("requests in 1..2000 and seconds >= 2 required")
    if not 1 <= args.history_models <= 5000 or not 0 <= args.batch_size <= args.requests:
        parser.error("history-models in 1..5000 and batch-size in 0..requests required")
    os.environ["HTD_PROBE_REFACTOR_HISTORY"] = "1"
    os.environ["HTD_PROBE_HISTORY_MODELS"] = str(args.history_models)
    directory = ROOT / ".verify/host-modules" / (args.label + "-" + uuid4().hex)
    report: dict[str, Any] = {
        "label": args.label,
        "build": "source host + production Vite + WebView2",
        "historyRequests": 5760,
        "historyModels": args.history_models,
        "historyQueryDays": 60,
        "requestsPerCollection": args.requests,
        "batchSize": args.batch_size or args.requests,
        "secondsPerScenario": args.seconds,
        "scenarios": {},
    }
    with UiProbe(directory) as probe:
        probe.command("state")
        probe.wait("!!document.querySelector('.harness-page')", timeout=30)

        def invoke(name: str, payload: dict[str, Any]) -> dict[str, Any]:
            probe.script(
                "window.__hostRead=null; pywebview.api.call("
                + json.dumps(name)
                + ","
                + json.dumps(payload)
                + ").then(r=>window.__hostRead={ok:r.ok,error:r.error,"
                "models:r.data?.totalModels,records:r.data?.totals?.recordCount})"
            )
            result = probe.wait("window.__hostRead")
            assert result["ok"], result
            return result

        def sample(name: str, action: Callable[[], object]) -> None:
            def run() -> None:
                action()

            value = measure(probe.process.pid, run, args.seconds)
            report["scenarios"][name] = value
            print(name, json.dumps(value["processMetrics"]), flush=True)

        def ingest(count: int) -> None:
            for index in range(args.requests):
                session = f"host-perf-{count}-{index % count}"
                path = directory / "codex" / (session + ".jsonl")
                if not path.exists():
                    rows = [json.loads(line) for line in headers().splitlines()]
                    rows[0]["payload"]["id"] = session
                    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                row = json.loads(request(index, Instant(datetime.now(UTC))))
                row["payload"]["thread_id"] = session
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row) + "\n")
                if (index + 1) % report["batchSize"] == 0 or index + 1 == args.requests:
                    invoke("collection.scanNow", {"operationId": str(uuid4())})

        sample("visible-idle", lambda: None)
        sample(f"single-session-{args.requests}", lambda: ingest(1))
        sample(f"multi-session-{args.requests}", lambda: ingest(8))
        now = datetime.now(UTC)
        query = {
            "start": format_instant(Instant(now - timedelta(days=60))),
            "end": format_instant(Instant(now)),
            "rangeKind": "all",
            "grain": "month",
            "sections": ["totals", "models", "trend", "quality"],
        }

        def history() -> None:
            for filters in ({}, {"harnesses": ["codex"]}, {"sections": ["totals", "models"]}):
                assert invoke("stats.explore", {**query, **filters})["models"] >= args.history_models

        sample("history-first-reads", history)
        sample(
            "history-page-hits",
            lambda: [invoke("stats.explore", {**query, "offset": offset}) for offset in (50, 100, 500, 1000)],
        )
        sample("wallet-reads", lambda: [invoke("wallet.list", {}) for _ in range(8)])
        probe.command("hide")
        sample("tray", lambda: None)
        probe.command("restore")
        sample("restored", lambda: None)
    with sqlite3.connect(directory / "htd.sqlite3") as db:
        report["acceptedRequests"] = db.execute("SELECT COUNT(*) FROM identity").fetchone()[0]
        assert report["acceptedRequests"] == 5764 + args.requests * 2, report["acceptedRequests"]
        assert db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0
    report["ok"] = True
    destination = ROOT / f".verify/host-modules-{args.label}.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
