"""Expose the common collector interface for the read-only Zcode database adapter."""

from htd.adapters.sqlite.zcode_source import read_zcode
from htd.application.ports import CheckpointRecord, ReadBatch
from htd.domain.common.time import Instant


class ZcodeReader:
    def read(self, path: str, checkpoint: CheckpointRecord, started_at: Instant) -> ReadBatch:
        return read_zcode(path, checkpoint, started_at)
