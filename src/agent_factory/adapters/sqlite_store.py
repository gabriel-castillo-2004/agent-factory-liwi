"""SQLite append-only EventStore adapter (INV-11, Section 7.5)."""

from pathlib import Path

from agent_factory.domain.events import Event


class SqliteEventStore:
    """SQLite-backed implementation of EventStore."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)

    def append(self, event: Event) -> None:
        """Append an event to the SQLite database."""
        raise NotImplementedError

    def get_events(self, project_id: str) -> list[Event]:
        """Return all events for project_id in append order."""
        raise NotImplementedError

    def list_project_ids(self) -> list[str]:
        """Return distinct project IDs stored in the database."""
        raise NotImplementedError

    def close(self) -> None:
        """Close any underlying database connection."""
        raise NotImplementedError
