"""Translate post-commit events and coalesce them until the visible bridge is ready."""

import json
import logging
from collections.abc import Callable
from concurrent.futures import Future
from threading import RLock
from typing import cast

from htd.application.ports import (
    CollectionCompleted,
    EventBus,
    ModelsChanged,
    OutputChanged,
    PreferencesChanged,
    PricingChanged,
    QualityChanged,
    SourcesChanged,
    TaskDone,
    TaskProgress,
    UsageChanged,
    WalletChanged,
)
from htd.ipc.dto.events import (
    CollectionRecord,
    CollectionTickEvent,
    EventRecord,
    RevisionChangedEvent,
    RevisionRecord,
    TaskDoneRecord,
    TaskProgressRecord,
    UsageChangedEvent,
    UsageRecord,
    VisibilityEvent,
    VisibilityRecord,
    WalletChangedEvent,
    WalletRecord,
)
from htd.ipc.dto.tasks import TaskDoneEvent, TaskProgressEvent

_LOG = logging.getLogger(__name__)


class EventForwarder:
    def __init__(
        self,
        bus: EventBus,
        *,
        collection_tick: Callable[[CollectionCompleted], CollectionTickEvent] | None = None,
    ) -> None:
        self._lock = RLock()
        self._pending: dict[str, EventRecord] = {}
        self._evaluate_js: Callable[[str], object] | None = None
        self._visible = True
        self._ready = False
        self._closed = False
        self._sending = False
        self._scheduled = False
        self._dispatch: Callable[[Callable[[], None]], None] | None = None
        self._collection_tick = collection_tick
        self._unsubscribe = bus.subscribe(self._receive)

    def _receive(self, event: object) -> None:
        records: list[EventRecord] = []
        if isinstance(event, TaskProgress):
            records.append(
                TaskProgressRecord(
                    payload=TaskProgressEvent(
                        taskId=event.task_id, completed=event.completed, total=event.total
                    )
                )
            )
        elif isinstance(event, TaskDone):
            records.append(
                TaskDoneRecord(
                    payload=TaskDoneEvent.model_validate(
                        {
                            "taskId": event.task_id,
                            "ok": event.ok,
                            "result": event.result,
                            "error": event.error,
                        }
                    )
                )
            )
        elif isinstance(event, UsageChanged):
            records.append(
                UsageRecord(
                    payload=UsageChangedEvent(
                        harnesses=[harness.value for harness in event.harnesses],
                        revision=event.revision,
                    )
                )
            )
        elif isinstance(event, WalletChanged):
            records.append(
                WalletRecord(
                    payload=WalletChangedEvent(vendorIds=list(event.vendor_ids), revision=event.revision)
                )
            )
        elif isinstance(event, (QualityChanged, ModelsChanged, OutputChanged)):
            records.append(
                RevisionRecord(
                    name="quality.changed"
                    if isinstance(event, QualityChanged)
                    else "output.changed"
                    if isinstance(event, OutputChanged)
                    else "models.changed",
                    payload=RevisionChangedEvent(revision=event.revision),
                )
            )
        elif isinstance(event, (PricingChanged, PreferencesChanged, SourcesChanged, CollectionCompleted)):
            name = (
                "pricing.changed"
                if isinstance(event, PricingChanged)
                else "preferences.changed"
                if isinstance(event, PreferencesChanged)
                else "sources.changed"
            )
            records.append(RevisionRecord(name=name, payload=RevisionChangedEvent(revision=event.revision)))
            if isinstance(event, CollectionCompleted) and self._collection_tick is not None:
                try:
                    records.append(CollectionRecord(payload=self._collection_tick(event)))
                except Exception:
                    _LOG.exception("Collection event snapshot failed")
        if records:
            with self._lock:
                if self._closed:
                    return
                for record in records:
                    self._pending[record.name] = record
                self._flush()

    def attach(
        self,
        evaluate_js: Callable[[str], object],
        *,
        dispatch: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        with self._lock:
            if self._closed:
                return
            self._evaluate_js = evaluate_js
            self._dispatch = dispatch
            self._ready = True
            self._flush()

    def detach(self) -> None:
        with self._lock:
            self._ready = False

    def collection_completed(self, payload: CollectionTickEvent) -> None:
        """Queue the actual scheduler completion snapshot before resolving manual callers."""
        with self._lock:
            if self._closed:
                return
            self._pending["collection.tick"] = CollectionRecord(payload=payload)
            self._flush()

    def set_visible(self, visible: bool) -> None:
        with self._lock:
            if self._closed or visible == self._visible:
                return
            self._visible = visible
            self._pending["app.visibility"] = VisibilityRecord(payload=VisibilityEvent(visible=visible))
            self._flush()

    def snapshot(self) -> list[EventRecord]:
        with self._lock:
            return [record.model_copy(deep=True) for record in self._pending.values()]

    def _flush(self) -> None:
        if self._sending or self._scheduled or self._closed or not self._visible or not self._ready:
            return
        if not self._pending:
            return
        if self._dispatch is not None:
            self._scheduled = True
            try:
                self._dispatch(self._deliver)
            except Exception:
                self._scheduled = False
                self._ready = False
                _LOG.exception("Bridge event dispatch failed")
        else:
            self._deliver()

    def _deliver(self) -> None:
        with self._lock:
            self._scheduled = False
            if self._sending or self._closed or not self._visible or not self._ready:
                return
            self._send()

    def _send(self) -> None:
        # Desktop delivery starts on the GUI thread and returns an observed async Future.
        evaluate_js = self._evaluate_js
        if evaluate_js is None:
            return
        self._sending = True
        asynchronous = False
        try:
            while self._pending and self._visible and self._ready and not self._closed:
                pending, self._pending = self._pending, {}
                data = json.dumps(
                    [record.model_dump(mode="json") for record in pending.values()],
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                script = (
                    "(() => { const events = "
                    + data
                    + "; for (const event of events) window.__htd.emit(event.name, event.payload); })()"
                )
                try:
                    result = evaluate_js(script)
                    if isinstance(result, Future):
                        asynchronous = True

                        def completed(
                            future: Future[object], batch: dict[str, EventRecord] = pending
                        ) -> None:
                            with self._lock:
                                try:
                                    future.result()
                                except Exception:
                                    if not self._closed:
                                        self._pending = {**batch, **self._pending}
                                        self._ready = False
                                    _LOG.exception("Bridge event delivery failed")
                                self._sending = False
                                self._flush()

                        cast(Future[object], result).add_done_callback(completed)
                        return
                except Exception:
                    # Keep latest payloads for retry; a failed WebView is unready until reattached.
                    self._pending = {**pending, **self._pending}
                    self._ready = False
                    _LOG.exception("Bridge event delivery failed")
        finally:
            if not asynchronous:
                self._sending = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._evaluate_js = None
            self._pending.clear()
        self._unsubscribe()
