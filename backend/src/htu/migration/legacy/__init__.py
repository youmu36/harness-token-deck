"""Read and merge the legacy SQLite representation without mutating it."""

from htu.migration.legacy.merge import LegacyMerged, merge_legacy
from htu.migration.legacy.reader import LegacySnapshot, LegacyVersion, open_legacy, read_legacy

__all__ = [
    "LegacyMerged",
    "LegacySnapshot",
    "LegacyVersion",
    "merge_legacy",
    "open_legacy",
    "read_legacy",
]
