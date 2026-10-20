"""Only public provider details can leave source configuration readers."""

import json
from pathlib import Path

from htu.adapters.collectors.provider_metadata import provider_metadata, public_url
from htu.domain.usage.model import Harness


def test_codex_public_names_and_urls_exclude_credentials(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[model_providers.shop]\nname="Shop"\nbase_url="https://shop.example/v1?api_key=secret"\napi_key="secret"\n',
        encoding="utf-8",
    )
    result = provider_metadata(Harness.CODEX, str(tmp_path / "sessions"))
    # Credential-bearing text is not shown even when it is embedded in an address.
    assert result == {"shop": ("Shop", None)}
    assert "secret" not in repr(result)
    assert public_url("https://shop.example/v1?ignored=value") is None
    assert public_url("https://SHOP.example:443/v1/") == "https://shop.example"
    assert public_url("https://user:secret@shop.example") is None


def test_zcode_singular_provider_config(tmp_path: Path) -> None:
    root = tmp_path / ".zcode"
    (root / "v2").mkdir(parents=True)
    (root / "v2/config.json").write_text(
        json.dumps(
            {"provider": {"shop-id": {"name": "Shop", "baseUrl": "https://shop.example", "apiKey": "secret"}}}
        ),
        encoding="utf-8",
    )
    result = provider_metadata(Harness.ZCODE, str(root / "cli/db/db.sqlite"))
    assert result == {"shop-id": ("Shop", "https://shop.example")}
    assert "secret" not in repr(result)
