"""Port definition for publishing ProjectStatus projections and registry index (INV-02, INV-11, INV-21)."""

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator

from agent_factory.domain.models import ProjectStatus, validate_project_id
from agent_factory.domain.states import ProjectState


class RegistryPublishError(RuntimeError):
    """Raised when publishing to the isolated registry branch fails, with redacted details (INV-19, AC-3.4)."""


class RegistryEntry(BaseModel):
    """Canonical project entry persisted in registry.json and projects/<project_id>.json on registry branch."""

    project_id: str
    name: str = ""
    objective: str = ""
    state: ProjectState
    progress_pct: int = Field(default=0, ge=0, le=100)
    repo_url: str | None = None
    deploy_url: str | None = None
    plan_hash: str | None = None
    updated_at: datetime | None = None

    @field_validator("project_id")
    @classmethod
    def _validate_project_id(cls, value: str) -> str:
        return validate_project_id(value)


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


@runtime_checkable
class RegistryPublisher(Protocol):
    """Protocol for publishing project metadata onto the isolated registry branch without modifying main (INV-21)."""

    def publish_project(self, status: ProjectStatus) -> RegistryEntry:
        """Update per-project JSON and registry.json index on the registry branch."""
        raise NotImplementedError

    def load_index(self) -> dict[str, RegistryEntry]:
        """Read all project entries currently stored in registry.json on the registry branch."""
        raise NotImplementedError
