"""Expose the common collector interface for the read-only Zcode database adapter."""

from htu.adapters.sqlite.zcode_source import read_zcode
from htu.application.ports import CheckpointRecord, ReadBatch
from htu.domain.common.time import Instant


class ZcodeReader:
    def read(self, path: str, checkpoint: CheckpointRecord, started_at: Instant) -> ReadBatch:
        return read_zcode(path, checkpoint, started_at)
