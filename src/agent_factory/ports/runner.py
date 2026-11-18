"""Port definition for command execution with timeout and process-tree cleanup (INV-02, INV-06)."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class CommandTimeoutError(TimeoutError):
    """Raised when a command exceeds its execution timeout and its process tree is killed."""


class CommandResult(BaseModel):
    """Result of executing an external command in a workspace."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    test_results: dict[str, bool] = Field(default_factory=dict)


@runtime_checkable
class Runner(Protocol):
    """Protocol for running commands inside an isolated workspace with hard timeouts."""

    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        timeout_seconds: float = 30.0,
        env: Mapping[str, str] | None = None,
        input: str | bytes | None = None,
    ) -> CommandResult:
        """Execute command in cwd and return CommandResult, or raise CommandTimeoutError."""
        raise NotImplementedError
