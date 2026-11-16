"""Port definition for version control, CI status, and deployment (INV-02, INV-15)."""

from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class CIRunResult(BaseModel):
    """Outcome of a remote CI verification round in a clean environment."""

    passed: bool
    summary: str = ""
    failed_checks: list[str] = Field(default_factory=list)
    repo_url: str | None = None
    deploy_url: str | None = None


@runtime_checkable
class VCS(Protocol):
    """Protocol for pushing work branches, querying CI, and merging/deploying."""

    def push_branch(
        self,
        project_id: str,
        workspace_dir: Path,
        branch: str = "work",
    ) -> str:
        """Push workspace_dir contents to the given branch and return a commit/push ref."""
        raise NotImplementedError

    def get_ci_result(
        self,
        project_id: str,
        branch: str = "work",
    ) -> CIRunResult:
        """Query the final CI judge result for the given project and branch."""
        raise NotImplementedError

    def merge_and_deploy(
        self,
        project_id: str,
        branch: str = "work",
    ) -> tuple[str, str]:
        """Merge verified branch and deploy static app, returning (repo_url, deploy_url)."""
        raise NotImplementedError
