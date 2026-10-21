"""Read public provider labels and URLs only; current config is not historical evidence."""

import json
import tomllib
from pathlib import Path
from typing import cast

from htd.domain.common.errors import DomainError
from htd.domain.common.redaction import redact_credentials
from htd.domain.common.site_url import site_url
from htd.domain.usage.model import Harness, optional_text


def public_text(value: object) -> str | None:
    text = optional_text(value)
    return text if text and redact_credentials(text) == text else None


def public_url(value: object) -> str | None:
    text = public_text(value)
    if not text:
        return None
    try:
        return site_url(text)
    except DomainError:
        return None


def provider_metadata(harness: Harness, path: str) -> dict[str, tuple[str | None, str | None]]:
    root = Path(path)
    result: dict[str, tuple[str | None, str | None]] = {}
    try:
        if harness == Harness.CODEX:
            config = root.parent / "config.toml"
            if config.stat().st_size > 4 * 1024**2:
                return result
            data = tomllib.loads(config.read_text(encoding="utf-8"))
            providers = data.get("model_providers", {})
        elif harness == Harness.ZCODE:
            config = root.parents[2] / "v2/config.json"
            if config.stat().st_size > 4 * 1024**2:
                return result
            data = json.loads(config.read_text(encoding="utf-8"))
            providers = data.get("provider", data.get("providers", {}))
        else:
            return result
        if isinstance(providers, list):
            providers = {
                cast(dict[str, object], p).get("id"): cast(dict[str, object], p)
                for p in cast(list[object], providers)
                if isinstance(p, dict)
            }
        if not isinstance(providers, dict):
            return result
        for identifier, value in cast(dict[object, object], providers).items():
            if type(value) is not dict or not (name := public_text(identifier)):
                continue
            fields = cast(dict[str, object], value)
            result[name] = (
                public_text(fields.get("name")),
                public_url(
                    fields.get("base_url")
                    or fields.get("baseUrl")
                    or fields.get("apiBase")
                    or fields.get("url")
                ),
            )
    except (OSError, ValueError, IndexError):
        pass
    return result
