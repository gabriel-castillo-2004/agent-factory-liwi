"""Port definition for the local code agent (INV-02, INV-09, INV-13, INV-18, ADR-002)."""

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from agent_factory.domain.models import AcceptanceTest, Plan


class CodeAgentError(Exception):
    """Base exception for code agent failures during test generation or implementation."""


class QuotaExhaustedError(CodeAgentError):
    """Raised when the code agent hits a plan/API quota limit and work must pause (INV-09, AC-2.4)."""

    def __init__(
        self,
        message: str = "Code agent quota exhausted",
        retry_after_seconds: float = 1.0,
    ) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class CodeAgentResult(BaseModel):
    """Summary of files created or modified by a single code agent invocation."""

    files_written: list[str] = Field(default_factory=list)
    summary: str = ""


@runtime_checkable
class CodeAgent(Protocol):
    """Protocol for local CLI or API code agents operating inside workspaces/<project_id>/."""

    def generate_tests(
        self,
        workspace_dir: Path,
        acceptance_tests: Sequence[AcceptanceTest],
    ) -> CodeAgentResult:
        """Generate test files from acceptance_tests ONLY, without access to production code (INV-13)."""
        raise NotImplementedError

    def implement_or_fix(
        self,
        workspace_dir: Path,
        plan: Plan,
        failure_output: str = "",
        attempt: int = 1,
    ) -> CodeAgentResult:
        """Write or fix production code in workspace_dir to satisfy the Plan and failing tests."""
        raise NotImplementedError
