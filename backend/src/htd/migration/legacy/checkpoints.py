"""Translate supported legacy checkpoints and describe records that cannot be resumed safely."""

import re
from typing import Any, cast

from htd.adapters.sqlite.usage_codec import dumps, object_json
from htd.domain.common.errors import DomainError
from htd.domain.sources.codex import CodexContext
from htd.domain.sources.zcode import ZcodeCheckpoint
from htd.domain.usage.model import optional_text


def convert_checkpoint(row: dict[str, Any]) -> str:
    state = cast(dict[str, Any], object_json(row["state_json"]))
    harness = row["harness"]
    if harness == "zcode":
        tail = state["tail_id"]
        if type(tail) is int:
            tail = str(tail)
        checkpoint = ZcodeCheckpoint(state["row_id"], tail, state["pending"])
        return dumps(checkpoint.to_dict())
    if type(state["context"]) is not dict:
        raise ValueError("Invalid checkpoint context")
    context = cast(dict[str, Any], state["context"])
    if type(state["diagnostics"]) is not list:
        raise ValueError("Invalid checkpoint diagnostics")
    diagnostics = cast(list[object], state["diagnostics"])
    if any(type(code) is not str or not code for code in diagnostics):
        raise ValueError("Invalid checkpoint diagnostics")
    if harness == "codex":
        offset, anchor = state["offset"], state["anchor"]
        if (
            type(offset) is not int
            or offset < 0
            or type(anchor) is not str
            or re.fullmatch(r"[0-9a-f]{64}", anchor) is None
        ):
            raise ValueError("Invalid checkpoint anchor")
        desktop = context["desktop"]
        if type(desktop) is not bool:
            raise ValueError("Invalid desktop marker")
        raw = context["session_id"]
        converted = CodexContext.from_dict(
            {
                "fileSessionId": optional_text(raw),
                "rawFileSessionId": raw,
                "sawSessionMeta": True,
                "originatorOk": desktop,
                "kind": "desktop" if desktop else None,
                "modelId": context["model_id"],
                "turnId": context["turn_id"],
                "sawRequestUsage": context["saw_request_usage"],
            }
        )
        return dumps(
            {"offset": offset, "anchor": anchor, "context": converted.to_dict(), "diagnostics": diagnostics}
        )
    if harness == "dsh":
        for name in ("session_id", "model_id"):
            value = context[name]
            if value is not None and (type(value) is not str or optional_text(value) != value):
                raise ValueError("Invalid DSH context text")
        return dumps(
            {
                "context": {
                    "sessionId": context["session_id"],
                    "rawSessionId": context["session_id"],
                    "modelId": context["model_id"],
                },
                "diagnostics": diagnostics,
            }
        )
    raise DomainError("UNKNOWN_HARNESS", "检查点来源无效", "harness")
