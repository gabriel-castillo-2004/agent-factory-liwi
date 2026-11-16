"""Local file adapter for StatusPublisher port (INV-11, Section 7.6)."""

from pathlib import Path

from agent_factory.domain.models import ProjectStatus
from agent_factory.domain.progress import project_status_from_events
from agent_factory.ports.store import EventStore


class FileStatusPublisher:
    """Publishes ProjectStatus projections as JSON files on local disk."""

    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _file_path(self, project_id: str) -> Path:
        return self.base_dir / f"{project_id}.json"

    def publish(self, status: ProjectStatus) -> None:
        """Write status JSON file for status.project_id."""
        path = self._file_path(status.project_id)
        path.write_text(status.model_dump_json(indent=2), encoding="utf-8")

    def load(self, project_id: str) -> ProjectStatus | None:
        """Read status JSON file for project_id, or return None if absent."""
        path = self._file_path(project_id)
        if not path.is_file():
            return None
        return ProjectStatus.model_validate_json(path.read_text(encoding="utf-8"))

    def delete(self, project_id: str) -> None:
        """Remove status JSON file for project_id if it exists."""
        path = self._file_path(project_id)
        if path.is_file():
            path.unlink()

    def rebuild_and_publish(self, project_id: str, store: EventStore) -> ProjectStatus:
        """Reconstruct ProjectStatus from EventStore and publish to disk (INV-11)."""
        events = store.get_events(project_id)
        status = project_status_from_events(project_id, events)
        self.publish(status)
        return status
