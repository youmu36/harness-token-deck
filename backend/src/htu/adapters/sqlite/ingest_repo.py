"""Commit atomic collection batches and persist bounded billing batches with restart-safe deduplication."""

import hashlib
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import asdict

from htu.adapters.sqlite.key_codec import key_bytes
from htu.adapters.sqlite.sources_repo import SourcesRepository
from htu.adapters.sqlite.transaction import require_transaction
from htu.adapters.sqlite.usage_codec import decode_event, dumps, encode_event
from htu.adapters.sqlite.usage_evidence import mark_compacted, supplement
from htu.application.ports import BillBatchResult, BilledUsage, CommitBatchResult, UnitOfWork
from htu.domain.common.errors import DomainError
from htu.domain.common.time import Instant, format_instant
from htu.domain.usage.model import Harness, UsageEvent
from htu.domain.usage.model_key import model_key


class IngestRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def commit_batch(
        self,
        work: UnitOfWork,
        harness: Harness,
        root: str,
        source_key: str,
        checkpoint: dict[str, object],
        events: Iterable[UsageEvent],
        *,
        at: Instant,
        compaction_keys: tuple[str, ...] = (),
    ) -> CommitBatchResult:
        require_transaction(work, self.connection)
        harness = Harness(harness)
        sources = SourcesRepository(self.connection)
        source = sources.begin_scan(work, harness, root, at=at)
        assert source.started_at is not None
        started_at, collected_at = format_instant(source.started_at), format_instant(at)
        inserted = duplicates = before_start = 0
        active_models: set[str] = set()
        for event in events:
            if type(event) is not UsageEvent or event.harness != harness:
                raise ValueError("Batch events must match the source Harness")
            if event.occurred_at < started_at:
                before_start += 1
                continue
            cursor = self.connection.execute(
                "INSERT INTO identity VALUES (?,?,?,0,?,'live') ON CONFLICT(harness,dedupe_key) DO NOTHING",
                (harness.value, key_bytes(event.dedupe_key), event.occurred_at, collected_at),
            )
            fact = dumps(
                [
                    event.occurred_at,
                    event.session_id,
                    event.request_id,
                    model_key(event.model_id) if event.model_id else None,
                    asdict(event.tokens),
                    event.status,
                ]
            )
            fingerprint = hashlib.sha256(fact.encode()).hexdigest()
            prior = self.connection.execute(
                "SELECT fingerprint,conflicted FROM source_identity_fact WHERE harness=? AND dedupe_key=?",
                (harness.value, key_bytes(event.dedupe_key)),
            ).fetchone()
            if prior is None and cursor.rowcount == 0:
                original = self.connection.execute(
                    "SELECT * FROM ledger WHERE harness=? AND dedupe_key=?",
                    (harness.value, key_bytes(event.dedupe_key)),
                ).fetchone()
                if original is not None:
                    from htu.domain.usage.model import Tokens

                    previous_fact = dumps(
                        [
                            original["occurred_at"],
                            original["session_id"],
                            original["request_id"],
                            original["model_key"] or None,
                            asdict(
                                Tokens(
                                    *(
                                        original[key]
                                        for key in (
                                            "input",
                                            "cache_read",
                                            "cache_write",
                                            "output",
                                            "reasoning",
                                        )
                                    )
                                )
                            ),
                            original["status"],
                        ]
                    )
                    previous_fingerprint = hashlib.sha256(previous_fact.encode()).hexdigest()
                    self.connection.execute(
                        "INSERT OR IGNORE INTO source_identity_fact VALUES(?,?,?,0)",
                        (harness.value, key_bytes(event.dedupe_key), previous_fingerprint),
                    )
                    prior = (previous_fingerprint, 0)
            compatible = supplement(
                self.connection, work, event, matching=prior is None or prior[0] == fingerprint
            )
            if prior is not None and compatible and prior[0] != fingerprint:
                # A proven missing-cache supplement establishes the richer source
                # fingerprint. Keep it after detail compaction so later replays
                # cannot turn this enrichment into a false identity conflict.
                self.connection.execute(
                    "UPDATE source_identity_fact SET fingerprint=? WHERE harness=? AND dedupe_key=?",
                    (fingerprint, harness.value, key_bytes(event.dedupe_key)),
                )
            if prior is not None and not compatible and not prior[1]:
                self.connection.execute(
                    "UPDATE source_identity_fact SET conflicted=1 WHERE harness=? AND dedupe_key=?",
                    (harness.value, key_bytes(event.dedupe_key)),
                )
                self.connection.execute(
                    "INSERT INTO diagnostic_log VALUES(NULL,?,?,'identity_conflict',?)",
                    (
                        collected_at,
                        harness.value,
                        event.dedupe_key + ": 同一身份的用量或模型冲突，原记录保留",
                    ),
                )
                work.touch("quality")
                work.touch("sources")
            self.connection.execute(
                "INSERT OR IGNORE INTO source_identity_fact VALUES(?,?,?,0)",
                (harness.value, key_bytes(event.dedupe_key), fingerprint),
            )
            if cursor.rowcount == 0:
                duplicates += 1
                continue
            self.connection.execute(
                "INSERT INTO inbox VALUES (?,?,?,?,?)",
                (
                    harness.value,
                    key_bytes(event.dedupe_key),
                    event.occurred_at,
                    encode_event(event),
                    collected_at,
                ),
            )
            inserted += 1
            active_models.add(model_key(event.model_id) if event.model_id else "")
        if harness == Harness.CODEX:
            for key in compaction_keys:
                mark_compacted(self.connection, work, key)
        sources.save_checkpoint(work, harness, source_key, checkpoint, at=at)
        if inserted:
            from htu.adapters.sqlite.token_activity import mark

            for model in active_models:
                mark(self.connection, harness.value, model, at)
            # Once per source batch, hold only accounts linked to this Harness.
            self.connection.execute(
                "UPDATE account_pending SET local_changed_at=?,reviewed_at=NULL WHERE segment_id IN "
                "(SELECT s.id FROM account_segment s JOIN vendor_link v ON v.vendor_id=s.vendor_id "
                "JOIN harness_selection h ON h.group_id=v.group_id WHERE s.ended_at IS NULL "
                "AND h.harness=? AND h.seq=(SELECT seq FROM harness_selection WHERE harness=h.harness "
                "ORDER BY effective_at DESC,seq DESC LIMIT 1) AND v.seq=(SELECT seq FROM vendor_link "
                "WHERE group_id=v.group_id ORDER BY effective_at DESC,seq DESC LIMIT 1))",
                (collected_at, harness.value),
            )
            self.connection.execute(
                "UPDATE collection_activity SET generation=generation+1,last_at=? WHERE id=1", (collected_at,)
            )
        return CommitBatchResult(inserted, duplicates, before_start)

    def bill_batch(
        self,
        work: UnitOfWork,
        calculate: Callable[[UsageEvent], BilledUsage | None],
        *,
        at: Instant,
        limit: int = 500,
        on_billed: Callable[[UnitOfWork, BilledUsage], None] | None = None,
    ) -> BillBatchResult:
        require_transaction(work, self.connection)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Billing batch limit must be between 1 and 500")
        # Bounded materialization allows the same connection to update/delete these rows safely.
        rows = self.connection.execute(
            "SELECT i.*,d.occurred_at identity_occurred_at,d.billed "
            "FROM inbox i LEFT JOIN identity d USING(harness,dedupe_key) "
            "ORDER BY i.occurred_at,i.harness,i.dedupe_key LIMIT ?",
            (limit,),
        ).fetchall()
        billed = corrupt = duplicates = 0
        harnesses: set[Harness] = set()
        output_samples: dict[tuple[str, str, str], list[tuple[str, int | None, int | None, str | None]]] = {}
        for row in rows:
            if (
                row["billed"] == 1
                or self.connection.execute(
                    "SELECT 1 FROM ledger WHERE harness=? AND dedupe_key=?",
                    (row["harness"], key_bytes(row["dedupe_key"])),
                ).fetchone()
            ):
                self._finish(row)
                duplicates += 1
                continue
            try:
                event = decode_event(row["event_json"])
                if (event.harness.value, event.dedupe_key, event.occurred_at) != (
                    row["harness"],
                    row["dedupe_key"],
                    row["occurred_at"],
                ) or event.occurred_at != row["identity_occurred_at"]:
                    raise ValueError("Inbox identity mismatch")
            except (ValueError, DomainError, RecursionError):
                self.connection.execute(
                    "INSERT INTO diagnostic_log VALUES(NULL,?,?,'inbox_corrupt',?)",
                    (
                        format_instant(at),
                        row["harness"],
                        row["dedupe_key"] + ": 请求用量或身份数据损坏，已跳过",
                    ),
                )
                self._finish(row)
                corrupt += 1
                continue
            priced = calculate(event)
            if priced is None:
                break
            if type(priced) is not BilledUsage or priced.event != event:
                raise ValueError("Billing result must preserve the inbox event")
            self._insert_ledger(priced, at)
            from htu.adapters.sqlite.session_hit_rate import record as record_session_hit_rate

            record_session_hit_rate(self.connection, event, at)
            key = (
                event.harness.value,
                model_key(event.model_id) if event.model_id else "",
                event.model_id or "模型未知",
            )
            output_samples.setdefault(key, []).append(
                (event.occurred_at, event.tokens.output, event.generation_milliseconds, event.session_id)
            )
            if priced.cost.unpriced or priced.cost.incomplete:
                work.touch("quality")
            if on_billed is not None:
                on_billed(work, priced)
            self._finish(row)
            billed += 1
            harnesses.add(event.harness)
        if billed or corrupt or duplicates:
            work.touch("sources")
        if billed:
            from htu.adapters.sqlite.output_speed import record
            from htu.adapters.sqlite.token_activity import mark

            for (source, model, name), samples in output_samples.items():
                record(
                    self.connection,
                    harness=source,
                    model_key=model,
                    display_name=name,
                    output=None,
                    occurred_at=samples[-1][0],
                    at=at,
                    samples=[(stamp, count, duration) for stamp, count, duration, _ in samples],
                    session_ids=[session for _, _, _, session in samples],
                )
                mark(self.connection, source, model, at)
            work.touch("usage")
            self.connection.execute(
                "UPDATE collection_activity SET generation=generation+1,last_at=? WHERE id=1",
                (format_instant(at),),
            )
        more = self.connection.execute("SELECT 1 FROM inbox LIMIT 1").fetchone() is not None
        return BillBatchResult(billed, corrupt, duplicates, more, tuple(h for h in Harness if h in harnesses))

    def _finish(self, row: sqlite3.Row) -> None:
        identity = row["harness"], key_bytes(row["dedupe_key"])
        self.connection.execute("UPDATE identity SET billed=1 WHERE harness=? AND dedupe_key=?", identity)
        self.connection.execute("DELETE FROM inbox WHERE harness=? AND dedupe_key=?", identity)

    def _insert_ledger(self, priced: BilledUsage, at: Instant) -> None:
        event, cost = priced.event, priced.cost
        tokens = event.tokens
        self.connection.execute(
            "INSERT INTO ledger VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.harness.value,
                key_bytes(event.dedupe_key),
                event.occurred_at,
                event.session_id,
                event.request_id,
                event.model_id,
                model_key(event.model_id) if event.model_id is not None else "",
                priced.display_name,
                event.source_kind,
                event.status,
                tokens.input,
                tokens.cache_read,
                tokens.cache_write,
                tokens.output,
                tokens.reasoning,
                dumps(event.issues),
                priced.group_id,
                priced.version_id,
                priced.revision_id,
                cost.rule_applied,
                str(cost.pico_usd),
                int(cost.incomplete),
                int(cost.unpriced),
                format_instant(at),
            ),
        )
