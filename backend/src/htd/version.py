"""One runtime version for the UI, database creation and frozen distribution."""

from importlib.metadata import version
from pathlib import Path


def app_version() -> str:
    source = Path(__file__).resolve().parents[3] / "VERSION"
    return source.read_text(encoding="utf-8").strip() if source.is_file() else version("harness-token-deck")
