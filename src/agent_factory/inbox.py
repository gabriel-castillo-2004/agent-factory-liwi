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
        return event_id in self._processed_ids

    def mark_processed(self, event_id: str) -> bool:
        """Register event_id as processed. Return True if new, False if duplicate."""
        if event_id in self._processed_ids:
            return False
        self._processed_ids.add(event_id)
        return True

    def process_once(self, event_id: str, handler: Callable[[], T]) -> T | None:
        """Mark event_id processed BEFORE calling handler; skip and return None if duplicate."""
        if self.is_processed(event_id):
            return None
        self.mark_processed(event_id)
        return handler()
