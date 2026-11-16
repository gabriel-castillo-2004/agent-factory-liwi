"""Port definition for publishing ProjectStatus projections (INV-02, INV-11, Section 7.6)."""

from typing import Protocol, runtime_checkable

from agent_factory.domain.models import ProjectStatus


@runtime_checkable
class StatusPublisher(Protocol):
    """Protocol for publishing and retrieving ProjectStatus projections."""

    def publish(self, status: ProjectStatus) -> None:
        """Persist or publish the latest ProjectStatus projection."""
        raise NotImplementedError

    def load(self, project_id: str) -> ProjectStatus | None:
        """Load the published ProjectStatus for a project_id, or None if missing."""
        raise NotImplementedError

    def delete(self, project_id: str) -> None:
        """Delete the published projection for a project_id."""
        raise NotImplementedError
