"""Canonical public website addresses shared by vendor discovery and account settings."""

from urllib.parse import urlsplit, urlunsplit

from htu.domain.common.errors import DomainError
from htu.domain.common.redaction import redact_credentials


def site_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        url = urlsplit(text)
        if (
            len(text) > 2048
            or url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or any(ord(c) <= 32 or c == "\\" for c in text)
            or redact_credentials(text) != text
        ):
            raise ValueError("Invalid website")
        host = url.hostname.lower()
        host = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
        port = url.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("Invalid port")
        if port and (url.scheme, port) not in (("http", 80), ("https", 443)):
            host += f":{port}"
        path = url.path.rstrip("/")
        if path.endswith("/v1"):
            path = path[:-3]
        return urlunsplit((url.scheme, host, path, "", ""))
    except (ValueError, UnicodeError):
        raise DomainError(
            "VENDOR_URL_INVALID", "供应商网址须为有效的 HTTP(S) 站点地址，不含凭据或查询参数", "url"
        ) from None
