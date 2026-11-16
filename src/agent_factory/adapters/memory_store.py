"""In-memory append-only EventStore adapter (INV-11, Section 7.5)."""

from agent_factory.domain.events import Event


class MemoryEventStore:
    """In-memory implementation of EventStore."""

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._seen_ids: set[str] = set()

    def append(self, event: Event) -> None:
        """Append an event to memory store."""
        raise NotImplementedError

    def get_events(self, project_id: str) -> list[Event]:
        """Return all events for project_id in append order."""
        raise NotImplementedError

    def list_project_ids(self) -> list[str]:
        """Return distinct project IDs in insertion order."""
        raise NotImplementedError
