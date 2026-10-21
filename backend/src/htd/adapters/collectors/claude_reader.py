"""Reuse anchored JSONL checkpointing without persisting Claude conversation text."""

from htd.adapters.collectors.codex_reader import CodexReader
from htd.domain.sources.claude import parse_record
from htd.domain.sources.codex import CodexContext
from htd.domain.usage.model import Result


class ClaudeReader(CodexReader):
    def parse(self, record: object, context: CodexContext) -> Result:
        return parse_record(record)

    def visible_diagnostics(self, diagnostics: set[str], context: CodexContext) -> tuple[str, ...]:
        return tuple(sorted(diagnostics))
