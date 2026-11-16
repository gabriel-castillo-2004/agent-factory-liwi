"""Local file adapter for StatusPublisher port (INV-11, Section 7.6)."""

from pathlib import Path

from agent_factory.domain.models import ProjectStatus
from agent_factory.ports.store import EventStore


class FileStatusPublisher:
    """Publishes ProjectStatus projections as JSON files on local disk."""

    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir)

    def publish(self, status: ProjectStatus) -> None:
        """Write status JSON file for status.project_id."""
        raise NotImplementedError

    def load(self, project_id: str) -> ProjectStatus | None:
        """Read status JSON file for project_id, or return None if absent."""
        raise NotImplementedError

    def delete(self, project_id: str) -> None:
        """Remove status JSON file for project_id if it exists."""
        raise NotImplementedError

    def rebuild_and_publish(self, project_id: str, store: EventStore) -> ProjectStatus:
        """Reconstruct ProjectStatus from EventStore and publish to disk (INV-11)."""
        raise NotImplementedError
