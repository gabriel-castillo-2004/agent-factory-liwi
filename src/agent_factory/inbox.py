"""Idempotent incoming event deduplication (INV-16, Section 7.7, AC-1.9)."""

from collections.abc import Callable, Iterable
from typing import TypeVar

T = TypeVar("T")


class Inbox:
    """Tracks processed incoming event IDs before any LLM invocation (INV-16)."""

    def __init__(self, processed_ids: Iterable[str] | None = None) -> None:
        self._processed_ids: set[str] = set(processed_ids or ())

    def is_processed(self, event_id: str) -> bool:
        """Return True if event_id has already been registered."""
        raise NotImplementedError

    def mark_processed(self, event_id: str) -> bool:
        """Register event_id as processed. Return True if new, False if duplicate."""
        raise NotImplementedError

    def process_once(self, event_id: str, handler: Callable[[], T]) -> T | None:
        """Mark event_id processed BEFORE calling handler; skip and return None if duplicate."""
        raise NotImplementedError
