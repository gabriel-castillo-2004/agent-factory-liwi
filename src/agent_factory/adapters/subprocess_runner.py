"""Subprocess command runner with strict timeout and process-tree termination (INV-02, INV-06, A2a-2)."""

from collections.abc import Sequence
from pathlib import Path

from agent_factory.ports.runner import CommandResult


class SubprocessRunner:
    """Executes local commands in a workspace and kills the entire process tree on timeout."""

    def __init__(self, default_timeout_seconds: float = 30.0) -> None:
        self.default_timeout_seconds = default_timeout_seconds

    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        timeout_seconds: float = 30.0,
    ) -> CommandResult:
        """Run command in cwd with timeout, killing parent and child processes if exceeded."""
        raise NotImplementedError
