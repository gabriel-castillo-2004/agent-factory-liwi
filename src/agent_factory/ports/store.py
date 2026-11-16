"""Port definition for append-only event storage (INV-02, INV-11, Section 7.5)."""

from typing import Protocol, runtime_checkable

from agent_factory.domain.events import Event


class DuplicateEventError(ValueError):
    """Raised when attempting to append an event with an ID that already exists."""


@runtime_checkable
class EventStore(Protocol):
    """Protocol for append-only event stores."""

    def append(self, event: Event) -> None:
        """Append an immutable event to the log."""
        raise NotImplementedError

    def get_events(self, project_id: str) -> list[Event]:
        """Return all events for a given project_id in append order."""
        raise NotImplementedError

    def list_project_ids(self) -> list[str]:
        """Return all distinct project IDs stored in the log."""
        raise NotImplementedError
