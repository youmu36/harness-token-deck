"""Check cancellation again when a queued write reaches its transaction boundary."""

from collections.abc import Callable


class CollectionInterrupted(Exception):
    """An unstarted collection batch was cancelled; committed batches remain valid."""


def check_cancelled(should_stop: Callable[[], bool]) -> None:
    if should_stop():
        raise CollectionInterrupted
