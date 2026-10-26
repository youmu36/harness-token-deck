"""Commit atomic collection batches and persist bounded billing batches with restart-safe deduplication."""

import hashlib
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import asdict

from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.session_core import observe
from htd.adapters.sqlite.sources_repo import SourcesRepository
from htd.adapters.sqlite.transaction import require_transaction
from htd.adapters.sqlite.usage_codec import decode_event, dumps, encode_event
from htd.adapters.sqlite.usage_evidence import mark_compacted, supplement
from htd.application.ports import BillBatchResult, BilledUsage, CommitBatchResult, UnitOfWork, UsageChanged
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.usage.model import OUTPUT_TIMING_UNCERTAIN, Harness, UsageEvent
from htd.domain.usage.model_key import model_key
from htd.domain.usage.session import UsageObservation


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
        watermark = hashlib.sha256(dumps(checkpoint).encode()).hexdigest()
        active_models: set[str] = set()
        sealed_due = False
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
                    from htd.domain.usage.model import Tokens

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
            if not sealed_due:
                # Freeze an elapsed account batch before admitting the eleventh-minute input.
                from htd.adapters.sqlite.account_book import AccountBook

                AccountBook(self.connection).begin_run(due_at=at)
                sealed_due = True
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
            observe(self.connection, UsageObservation(event, at, watermark))
            from htd.adapters.sqlite.session_activity import received as activity_received
            from htd.adapters.sqlite.session_projection import received as usage_received

            reference = self.connection.execute(
                "SELECT session_ref FROM session_request WHERE harness=? AND dedupe_key=?",
                (harness.value, key_bytes(event.dedupe_key)),
            ).fetchone()[0]
            activity_received(self.connection, UsageObservation(event, at, watermark), reference)
            usage_received(self.connection, event)
            active_models.add(model_key(event.model_id) if event.model_id else "")
        if harness == Harness.CODEX:
            for key in compaction_keys:
                mark_compacted(self.connection, work, key)
        sources.save_checkpoint(work, harness, source_key, checkpoint, at=at)
        if harness == Harness.CODEX and isinstance(checkpoint.get("context"), dict):
            from typing import cast

            from htd.adapters.sqlite.session_core import directory

            context = cast(dict[str, object], checkpoint["context"])
            native = context.get("rawFileSessionId")
            if isinstance(native, str) and context.get("originatorOk") and context.get("kind"):
                ref = directory(self.connection, harness, native, occurred_at=None, received_at=None)
                if ref is not None:
                    self.connection.execute(
                        "INSERT INTO session_weak_activity VALUES(?,?,?,?) "
                        "ON CONFLICT(session_ref,source_key) DO UPDATE SET "
                        "weak=excluded.weak,checked_at=excluded.checked_at",
                        (ref, source_key, int(context.get("requestStartedAt") is not None), collected_at),
                    )
        if inserted:
            work.touch("usage")
            work.emit(UsageChanged((harness,), work.revision("usage")))
            from htd.adapters.sqlite.token_activity import mark

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
        output_samples: dict[
            tuple[str, str, str], list[tuple[str, int | None, int | None, str | None, tuple[str, str, str]]]
        ] = {}
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
                self._finish(row, state="corrupt")
                corrupt += 1
                continue
            priced = calculate(event)
            if priced is None:
                break
            if type(priced) is not BilledUsage or priced.event != event:
                raise ValueError("Billing result must preserve the inbox event")
            self._insert_ledger(priced, at)
            observed = self.connection.execute(
                "SELECT first_seen_at,origin FROM identity WHERE harness=? AND dedupe_key=?",
                (event.harness.value, key_bytes(event.dedupe_key)),
            ).fetchone()
            if observed is not None:
                observe(
                    self.connection, UsageObservation(event, parse_instant(observed[0]), "inbox"), valued=True
                )
            self.connection.execute(
                "UPDATE session_request SET valued_revision=revision,state='billed' "
                "WHERE harness=? AND dedupe_key=?",
                (event.harness.value, key_bytes(event.dedupe_key)),
            )
            from htd.adapters.sqlite.session_hit_rate import record as record_session_hit_rate

            record_session_hit_rate(self.connection, event, parse_instant(observed[0]) if observed else at)
            if on_billed is not None:
                on_billed(work, priced)
            from htd.adapters.sqlite.usage_attribution import vendor_for_request

            vendor = vendor_for_request(
                self.connection,
                event.harness.value,
                key_bytes(event.dedupe_key),
                priced.group_id,
                event.occurred_at,
            )
            from htd.adapters.sqlite.session_projection import valued

            valued(
                self.connection,
                event,
                priced.cost.pico_usd,
                (priced.group_id or "", priced.version_id or "", vendor),
            )
            key = (
                event.harness.value,
                model_key(event.model_id) if event.model_id else "",
                event.model_id or "模型未知",
            )
            output_samples.setdefault(key, []).append(
                (
                    event.occurred_at,
                    # Keep billing intact, but break inferred timing continuity
                    # instead of treating ambiguous interaction time as a minute.
                    None if OUTPUT_TIMING_UNCERTAIN in event.issues else event.tokens.output,
                    event.generation_milliseconds,
                    event.session_id,
                    (vendor, priced.group_id or "", priced.version_id or ""),
                )
            )
            if priced.cost.unpriced or priced.cost.incomplete:
                work.touch("quality")
            self._finish(row)
            billed += 1
            harnesses.add(event.harness)
        if billed or corrupt or duplicates:
            work.touch("sources")
        if billed:
            from htd.adapters.sqlite.output_speed import record

            for (source, model, name), samples in output_samples.items():
                record(
                    self.connection,
                    harness=source,
                    model_key=model,
                    display_name=name,
                    output=None,
                    occurred_at=samples[-1][0],
                    at=at,
                    samples=[(stamp, count, duration) for stamp, count, duration, _, _ in samples],
                    session_ids=[session for _, _, _, session, _ in samples],
                    attributions=[attribution for _, _, _, _, attribution in samples],
                )
            work.touch("usage")
        more = self.connection.execute("SELECT 1 FROM inbox LIMIT 1").fetchone() is not None
        return BillBatchResult(billed, corrupt, duplicates, more, tuple(h for h in Harness if h in harnesses))

    def _finish(self, row: sqlite3.Row, *, state: str = "billed") -> None:
        identity = row["harness"], key_bytes(row["dedupe_key"])
        self.connection.execute("UPDATE identity SET billed=1 WHERE harness=? AND dedupe_key=?", identity)
        self.connection.execute("DELETE FROM inbox WHERE harness=? AND dedupe_key=?", identity)
        self.connection.execute(
            "UPDATE session_request SET state=? WHERE harness=? AND dedupe_key=?", (state, *identity)
        )

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
