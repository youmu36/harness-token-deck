"""Expose only stable user messages; retain unexpected exception details in logs."""

import logging
import traceback
from uuid import uuid4

from pydantic import JsonValue, ValidationError
from pydantic.alias_generators import to_camel

from htd.domain.common.errors import DomainError
from htd.domain.common.redaction import redact_credentials
from htd.ipc.dto.base import Error, Failure

_LOG = logging.getLogger(__name__)
_RETRYABLE = {"WRITE_TIMEOUT", "READ_TIMEOUT", "SERVER_BUSY", "APP_SHUTTING_DOWN"}


def failure(error: Error) -> dict[str, JsonValue]:
    # Omit absent envelope metadata, but never remove nulls from successful DTO data.
    return Failure(error=error).model_dump(mode="json", exclude_none=True)


def invalid_request(error: ValidationError) -> dict[str, JsonValue]:
    location = error.errors(include_url=False, include_context=False, include_input=False)[0]["loc"]
    field = ".".join(str(part) for part in location) or "payload"
    return failure(
        Error(code="INVALID_REQUEST", message="参数格式无效，请检查输入", field=field, retryable=False)
    )


def exception_envelope(error: Exception) -> dict[str, JsonValue]:
    if isinstance(error, DomainError):
        try:
            field = ".".join(to_camel(part) for part in error.field.split(".")) if error.field else None
            return failure(
                Error(
                    code=error.code,
                    message=redact_credentials(error.message),
                    field=field,
                    retryable=error.retryable or error.code in _RETRYABLE,
                    conflictRevision=error.conflict_revision,
                )
            )
        except (ValidationError, TypeError, AttributeError):
            pass  # Invalid error metadata is an internal failure, never an escaping bridge exception.
    log_id = uuid4().hex
    details = redact_credentials("".join(traceback.format_exception(error)))
    _LOG.error("IPC internal error logId=%s\n%s", log_id, details)
    return failure(
        Error(
            code="INTERNAL_ERROR",
            message=f"发生内部错误（日志编号 {log_id}）",  # noqa: RUF001 - Protocol user-facing text.
            retryable=False,
            logId=log_id,
        )
    )
