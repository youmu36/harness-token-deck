"""Remove credential patterns before messages reach persistence or diagnostics."""

import re
from collections.abc import Iterable

_SECRET = re.compile(
    r"(?i)(?:bearer\s+[^\s\"',;}]+|sk-[A-Za-z0-9_-]+|"
    r"[\"']?(?:api[_-]?key|access[_-]?token|authorization)[\"']?\s*[:=]\s*"
    r"(?:[\"'][^\"'\r\n]*[\"']|(?:bearer\s+)?[^\s,;}]+))"
)


def redact_credentials(message: str, secrets: Iterable[str] = ()) -> str:
    for secret in sorted((value for value in secrets if value), key=len, reverse=True):
        message = message.replace(secret, "[redacted]")
    return _SECRET.sub("[redacted]", message)
