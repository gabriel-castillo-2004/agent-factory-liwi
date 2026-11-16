"""Scripted fake command runner adapter for offline deterministic testing (INV-02, INV-03)."""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from agent_factory.ports.runner import CommandResult

RunnerStep = CommandResult | Exception | Callable[[Sequence[str], Path], CommandResult]


class FakeRunner:
    """Fake command runner returning scripted CommandResults in FIFO order."""

    def __init__(self, results: Sequence[RunnerStep] | None = None) -> None:
        self._results: list[RunnerStep] = list(results or [])
        self.calls: list[dict[str, Any]] = []

    def enqueue(self, step: RunnerStep) -> None:
        """Append a scripted CommandResult or Exception to the queue."""
        raise NotImplementedError

    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        timeout_seconds: float = 30.0,
    ) -> CommandResult:
        """Record call and return next scripted CommandResult or raise scripted Exception."""
        raise NotImplementedError
