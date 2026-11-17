"""Scripted fake command runner adapter for offline deterministic testing (INV-02, INV-03)."""

from collections.abc import Callable, Mapping, Sequence
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
        self._results.append(step)

    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        timeout_seconds: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Record call and return next scripted CommandResult or raise scripted Exception."""
        self.calls.append(
            {
                "command": list(command),
                "cwd": Path(cwd),
                "timeout_seconds": timeout_seconds,
                "env": dict(env) if env is not None else None,
            }
        )
        if not self._results:
            return CommandResult(exit_code=0, stdout="", stderr="", timed_out=False)
        step = self._results.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, type) and issubclass(step, Exception):
            raise step()
        if callable(step):
            return step(command, cwd)
        if isinstance(step, CommandResult):
            return step
        return CommandResult(exit_code=0, stdout=str(step))
