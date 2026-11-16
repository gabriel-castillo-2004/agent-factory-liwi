"""In-memory append-only EventStore adapter (INV-11, Section 7.5)."""

from agent_factory.domain.events import Event
from agent_factory.ports.store import DuplicateEventError


class MemoryEventStore:
    """In-memory implementation of EventStore."""

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._seen_ids: set[str] = set()

    def append(self, event: Event) -> None:
        """Append an event to memory store."""
        if event.id in self._seen_ids:
            raise DuplicateEventError(f"Duplicate event ID: {event.id}")
        self._seen_ids.add(event.id)
        self._events.append(event)

    def get_events(self, project_id: str) -> list[Event]:
        """Return all events for project_id in append order."""
        return [e for e in self._events if e.project_id == project_id]

    def list_project_ids(self) -> list[str]:
        """Return distinct project IDs in insertion order."""
        seen: set[str] = set()
        res: list[str] = []
        for e in self._events:
            if e.project_id not in seen:
                seen.add(e.project_id)
                res.append(e.project_id)
        return res
