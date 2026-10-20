"""Request-level cache warnings, shared by live aggregation and historical compaction."""


def cache_warnings(
    input_tokens: int | None,
    cache_read: int | None,
    *,
    session: bool,
    unprobed: bool = False,
    compacted: bool = False,
) -> tuple[bool, bool]:
    if compacted or unprobed or input_tokens is None or cache_read is None or input_tokens < 50_000:
        return False, False
    bust = session and cache_read * 10 < input_tokens
    return bust, not bust and input_tokens - cache_read >= 50_000
