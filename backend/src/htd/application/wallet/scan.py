"""Read recent provider identifiers and atomically create named, explicit aliases."""

import hashlib
from collections.abc import Callable
from datetime import timedelta
from typing import cast

from htd.application.command import execute
from htd.application.ports import (
    CheckpointRecord,
    SourceDiscovery,
    SourceReader,
    TaskContext,
    TaskControl,
    UnitOfWork,
    WalletCommandStore,
)
from htd.application.wallet import Wallet
from htd.domain.common.errors import DomainError
from htd.domain.common.redaction import redact_credentials
from htd.domain.common.site_url import site_url
from htd.domain.common.time import Clock, Instant, format_instant, parse_instant
from htd.domain.usage.model import Harness


class ProviderScan:
    def __init__(
        self,
        store: WalletCommandStore,
        discovery: SourceDiscovery,
        readers: dict[Harness, SourceReader],
        tasks: TaskControl,
        clock: Clock,
        wallet: Wallet,
        metadata: Callable[[Harness, str], dict[str, tuple[str | None, str | None]]],
    ) -> None:
        self.store, self.discovery, self.readers, self.tasks, self.clock, self.wallet, self.metadata = (
            store,
            discovery,
            readers,
            tasks,
            clock,
            wallet,
            metadata,
        )

    def start(self, operation_id: str) -> dict[str, object]:
        reserved: list[str] = []

        def reserve(work: UnitOfWork, at: Instant) -> dict[str, object]:
            task_id = self.tasks.reserve()
            reserved.append(task_id)
            return {"taskId": task_id}

        try:
            result = execute(self.store, self.clock, "wallet.scanProviders", {}, operation_id, reserve)
        except BaseException:
            for task_id in reserved:
                self.tasks.discard(task_id)
            raise
        if reserved:
            self.tasks.start(cast(str, result["taskId"]), self.run)
        return result

    def run(self, context: TaskContext) -> dict[str, object]:
        end = self.clock.now_utc()
        start = Instant(end.value - timedelta(days=3))
        files: list[tuple[Harness, str, dict[str, tuple[str | None, str | None]]]] = []
        diagnostics: set[str] = set()
        for source in self.store.sources().sources:
            context.check_cancelled()
            found = self.discovery.discover(source.harness, source.path)
            diagnostics.update(found.diagnostics)
            details = self.metadata(source.harness, source.path)
            files.extend((source.harness, path, details) for path in found.paths)
        candidates: dict[str, dict[str, object]] = {}
        wallets, _ = self.store.wallets(at=end, include_archived=True)
        existing_urls = {snapshot.vendor.url for snapshot in wallets if snapshot.vendor.url}
        missing = 0
        seen: set[tuple[Harness, str]] = set()
        for index, (harness, path, metadata) in enumerate(files):
            context.progress(index, len(files))
            checkpoint = CheckpointRecord()
            while True:
                context.check_cancelled()
                batch = self.readers[harness].read(path, checkpoint, start)
                diagnostics.update(batch.diagnostics)
                for event in batch.events:
                    identity = (harness, event.dedupe_key)
                    if identity in seen or not start <= parse_instant(event.occurred_at) < end:
                        continue
                    seen.add(identity)
                    name = event.provider_name
                    if not name or redact_credentials(name) != name:
                        missing += 1
                        continue
                    label, url = metadata.get(name, (None, None))
                    from_config = bool(url)
                    try:
                        url = site_url(url or name)
                    except DomainError:
                        url = None
                    if not url:
                        missing += 1
                        continue
                    if url in existing_urls:
                        continue
                    identifier = hashlib.sha256(url.encode()).hexdigest()
                    candidates.setdefault(
                        identifier,
                        {
                            "id": identifier,
                            "identifier": url,
                            "harness": harness.value,
                            "label": label,
                            "url": url,
                            "fromCurrentConfig": from_config,
                            "vendorId": None,
                        },
                    )
                    if len(candidates) > 200:
                        raise DomainError("VENDOR_LIMIT", "近三天来源标识超过 200 个，请缩小来源范围")
                if not batch.more or batch.retry or not batch.available:
                    break
                if batch.checkpoint == checkpoint.state:
                    diagnostics.add("source_scan_stalled")
                    break
                checkpoint = CheckpointRecord(batch.checkpoint, batch.diagnostics)
        context.progress(len(files), len(files))
        return {
            "kind": "providers",
            "start": format_instant(start),
            "end": format_instant(end),
            "providers": sorted(candidates.values(), key=lambda p: cast(str, p["identifier"]).casefold()),
            "missingProviderCount": missing,
            "diagnostics": sorted(diagnostics),
        }

    def create(self, task_id: str, rows: list[dict[str, str]], operation_id: str) -> dict[str, object]:
        parameters: dict[str, object] = {"taskId": task_id, "providers": rows}

        def prepare() -> dict[str, object]:
            snapshot = self.tasks.get(task_id)
            result = cast(dict[str, object] | None, snapshot.get("result"))
            if snapshot["status"] != "completed" or not result or result.get("kind") != "providers":
                raise DomainError("PROVIDER_SCAN_INVALID", "请先完成供应商扫描")
            available = {p["id"]: p for p in cast(list[dict[str, object]], result["providers"])}
            if (
                not rows
                or len({r["id"] for r in rows}) != len(rows)
                or any(r["id"] not in available or not r["name"].strip() for r in rows)
            ):
                raise DomainError("PROVIDER_SCAN_INVALID", "每个选中的来源须填写供应商名称")
            return {
                "providers": [
                    {
                        "name": row["name"].strip(),
                        "url": available[row["id"]]["url"],
                    }
                    for row in rows
                ]
            }

        return self.wallet.change("wallet.createScannedVendors", parameters, operation_id, prepare=prepare)
