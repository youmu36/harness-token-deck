import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import cast

import pytest
from htu.domain.common.errors import (
    SOURCE_BATCH_INVALID,
    SOURCE_CONTEXT_INVALID,
    SOURCE_ROW_INVALID,
    DomainError,
)
from htu.domain.common.time import from_epoch_ms
from htu.domain.sources.zcode import ZcodeCheckpoint, ZcodeRow, advance
from htu.domain.usage.model import Diagnostic

START = from_epoch_ms(1788220800000)


def row(number: int, **changes: object) -> ZcodeRow:
    return replace(
        ZcodeRow(
            number,
            f"row-{number}",
            "s",
            f"r-{number}",
            status="completed",
            completed_at=1788220801000,
            raw_usage_json='{"inputTokens":100,"cacheReadTokens":40,"outputTokens":10}',
        ),
        **changes,
    )


def test_C09_running_to_cancelled_and_retry() -> None:
    initial = ZcodeCheckpoint()
    first = advance(initial, {}, [row(1, status="running", completed_at=None)], START)
    assert first.checkpoint == ZcodeCheckpoint(1, "row-1", (1,))
    assert first.events == first.diagnostics == ()
    result = advance(
        first.checkpoint,
        {1: row(1, status="cancelled", raw_usage_json=None)},
        [row(2, logical_request_id="r-1", attempt_index=1)],
        START,
    )
    assert len(result.events) == 2
    assert result.events[0].status == "cancelled"
    assert result.events[0].tokens.input is None
    assert result.events[0].tokens.cache_read is None
    assert result.events[1].tokens.input == 100
    assert result.events[0].dedupe_key != result.events[1].dedupe_key
    assert result.checkpoint == ZcodeCheckpoint(2, "row-2")
    assert initial == ZcodeCheckpoint()


def test_C10_terminal_matrix() -> None:
    path = Path(__file__).resolve().parents[4] / "fixtures/zcode/terminal-cases.json"
    cases = cast(list[dict[str, object]], json.loads(path.read_text("utf-8")))
    rows = [
        ZcodeRow(
            index,
            cast(str, case["id"]),
            session_id=case["session_id"],
            logical_request_id=case["logical_request_id"],
            attempt_index=case["attempt_index"],
            model_id=case["model_id"],
            status=case["status"],
            completed_at=case["completed_at"],
            raw_usage_json=case["raw_usage_json"],
        )
        for index, case in enumerate(cases, 1)
    ]
    result = advance(ZcodeCheckpoint(), {}, rows, START)
    assert len(result.events) == 4
    assert [event.status for event in result.events] == ["completed", "error", "completed", "cancelled"]
    assert result.events[1].tokens.input == 50
    assert result.events[1].dedupe_key != result.events[2].dedupe_key
    assert result.checkpoint == ZcodeCheckpoint(6, "unknown-terminal", ())
    assert result.diagnostics == (Diagnostic("invalid_token_count"), Diagnostic("source_status_unsupported"))
    corrected = advance(
        ZcodeCheckpoint(result.checkpoint.row_id, result.checkpoint.tail_id, (5, 6)),
        {5: replace(rows[4], raw_usage_json="{}"), 6: replace(rows[5], status="completed")},
        [],
        START,
    )
    assert len(corrected.events) == 2
    assert corrected.checkpoint.pending == ()
    assert corrected.diagnostics == ()


def test_C08_two_pages_of_520_in_memory_rows() -> None:
    rows = [row(number) for number in range(1, 521)]
    first = advance(ZcodeCheckpoint(), {}, rows[:500], START)
    assert len(first.events) == 500
    assert first.more
    assert first.checkpoint == ZcodeCheckpoint(500, "row-500")
    restored = ZcodeCheckpoint.from_dict(json.loads(json.dumps(first.checkpoint.to_dict())))
    second = advance(restored, {}, rows[500:], START)
    assert len(second.events) == 20
    assert not second.more
    assert second.checkpoint == ZcodeCheckpoint(520, "row-520")
    assert len({event.dedupe_key for event in first.events + second.events}) == 520


def test_pending_does_not_consume_page_quota_and_is_processed_first() -> None:
    checkpoint = ZcodeCheckpoint(1, "row-1", (1,))
    result = advance(checkpoint, {1: row(1)}, [row(number) for number in range(2, 502)], START)
    assert len(result.events) == 501
    assert result.events[0].request_id == "r-1"
    assert result.more


def test_full_pending_page_still_signals_more() -> None:
    result = advance(ZcodeCheckpoint(), {}, [row(n, status="running") for n in range(1, 501)], START)
    assert result.events == result.diagnostics == ()
    assert len(result.checkpoint.pending) == 500
    assert result.more


def test_deleted_pending_reports_only_once() -> None:
    checkpoint = ZcodeCheckpoint(2, "row-2", (2, 1, 1))
    result = advance(checkpoint, {1: None, 2: row(2, status="running")}, [], START)
    assert result.diagnostics == (Diagnostic("pending_request_removed"),)
    assert result.checkpoint == ZcodeCheckpoint(2, "row-2", (2,))
    again = advance(result.checkpoint, {2: row(2, status="running")}, [], START)
    assert again.diagnostics == ()


def test_start_boundary_and_invalid_old_rows() -> None:
    cutoff = from_epoch_ms(1788220801000)
    result = advance(
        ZcodeCheckpoint(),
        {},
        [
            row(1, completed_at=1788220800999),
            row(2),
            row(3, completed_at=1788220800999, raw_usage_json="bad"),
        ],
        cutoff,
    )
    assert [event.request_id for event in result.events] == ["r-2"]
    assert result.checkpoint.pending == ()
    assert result.diagnostics == (Diagnostic("invalid_usage_json"),)
    rechecked = advance(
        ZcodeCheckpoint(3, "row-3", (3,)), {3: row(3, completed_at=1788220800999)}, [], cutoff
    )
    assert rechecked.events == ()
    assert rechecked.checkpoint.pending == ()


def test_nonterminal_precedence_and_checkpoint_tail() -> None:
    result = advance(
        ZcodeCheckpoint(),
        {},
        [
            row(1, status="future", completed_at=None),
            row(2, status="running"),
            row(3, status="error", completed_at=None),
        ],
        START,
    )
    assert result.checkpoint == ZcodeCheckpoint(3, "row-3", (1, 2, 3))
    assert result.diagnostics == result.events == ()


def test_empty_batch_keeps_checkpoint() -> None:
    checkpoint = ZcodeCheckpoint(4, "raw tail id ")
    result = advance(checkpoint, {}, [], START)
    assert result.checkpoint == checkpoint
    assert result.events == result.diagnostics == ()
    assert not result.more


@pytest.mark.parametrize(
    "fresh", [[row(2), row(1)], [row(1), row(1)], [row(1)], [row(n) for n in range(2, 503)]]
)
def test_rejects_invalid_fresh_batch(fresh: list[ZcodeRow]) -> None:
    with pytest.raises(DomainError) as failure:
        advance(ZcodeCheckpoint(1, "row-1"), {}, fresh, START)
    assert failure.value.code == SOURCE_BATCH_INVALID


@pytest.mark.parametrize("pending", [{}, {1: row(2)}, {1: None, 2: row(2)}, {True: row(1)}])
def test_rejects_incomplete_or_mismatched_recheck(pending: dict[int, ZcodeRow | None]) -> None:
    with pytest.raises(DomainError) as failure:
        advance(ZcodeCheckpoint(1, "row-1", (1,)), pending, [], START)
    assert failure.value.code == SOURCE_BATCH_INVALID


@pytest.mark.parametrize(
    ("row_id", "tail", "pending"),
    [
        (-1, None, ()),
        (True, "id", ()),
        (0, "id", ()),
        (1, None, ()),
        (1, "", ()),
        (1, "id", (2,)),
        (1, "id", (0,)),
        (1, "id", (True,)),
        (1, "id", None),
        (1, "id", "1"),
    ],
)
def test_invalid_checkpoints(row_id: object, tail: object, pending: object) -> None:
    with pytest.raises(DomainError) as failure:
        ZcodeCheckpoint.from_dict({"rowId": row_id, "tailId": tail, "pending": pending})
    assert failure.value.code == SOURCE_CONTEXT_INVALID


@pytest.mark.parametrize("data", [None, [], {}, {"rowId": 0, "tailId": None}])
def test_missing_checkpoint_fields(data: object) -> None:
    with pytest.raises(DomainError) as failure:
        ZcodeCheckpoint.from_dict(cast(dict[str, object], data))
    assert failure.value.code == SOURCE_CONTEXT_INVALID


@pytest.mark.parametrize(("rowid", "identity"), [(0, "x"), (-1, "x"), (True, "x"), (1, ""), (1, None)])
def test_invalid_row_locators(rowid: object, identity: object) -> None:
    with pytest.raises(DomainError) as failure:
        ZcodeRow(cast(int, rowid), cast(str, identity))
    assert failure.value.code == SOURCE_ROW_INVALID


def test_checkpoints_and_batch_are_immutable() -> None:
    pending = [2, 1, 1]
    checkpoint = ZcodeCheckpoint.from_dict({"rowId": 2, "tailId": "tail", "pending": pending})
    pending.clear()
    assert checkpoint.pending == (1, 2)
    assert ZcodeCheckpoint.from_dict(json.loads(json.dumps(checkpoint.to_dict()))) == checkpoint
    with pytest.raises(FrozenInstanceError):
        checkpoint.row_id = 3  # pyright: ignore[reportAttributeAccessIssue]
    batch = advance(ZcodeCheckpoint(), {}, [row(1)], START)
    with pytest.raises(FrozenInstanceError):
        batch.more = True  # pyright: ignore[reportAttributeAccessIssue]
    assert not hasattr(batch, "__dict__")
