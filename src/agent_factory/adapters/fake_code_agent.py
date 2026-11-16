"""Scripted fake code agent adapter for offline deterministic testing (INV-02, INV-13, ADR-002)."""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from agent_factory.domain.models import AcceptanceTest, Plan
from agent_factory.ports.code_agent import CodeAgentResult

TestGenStep = (
    dict[str, str]
    | Exception
    | Callable[[Path, Sequence[AcceptanceTest]], CodeAgentResult]
)
ImplStep = (
    dict[str, str]
    | Exception
    | Callable[[Path, Plan, str, int], CodeAgentResult]
)


class FakeCodeAgent:
    """Fake code agent executing scripted file writes or raising scripted exceptions."""

    def __init__(
        self,
        test_gen_steps: Sequence[TestGenStep] | None = None,
        impl_steps: Sequence[ImplStep] | None = None,
    ) -> None:
        self._test_gen_steps: list[TestGenStep] = list(test_gen_steps or [])
        self._impl_steps: list[ImplStep] = list(impl_steps or [])
        self.test_gen_calls: list[dict[str, Any]] = []
        self.impl_calls: list[dict[str, Any]] = []

    def enqueue_test_gen(self, step: TestGenStep) -> None:
        """Append a scripted test generation step."""
        raise NotImplementedError

    def enqueue_impl(self, step: ImplStep) -> None:
        """Append a scripted implementation/fix step."""
        raise NotImplementedError

    def generate_tests(
        self,
        workspace_dir: Path,
        acceptance_tests: Sequence[AcceptanceTest],
    ) -> CodeAgentResult:
        """Execute next scripted test-generation step using ONLY acceptance_tests (A2a-5)."""
        raise NotImplementedError

    def implement_or_fix(
        self,
        workspace_dir: Path,
        plan: Plan,
        failure_output: str = "",
        attempt: int = 1,
    ) -> CodeAgentResult:
        """Execute next scripted implementation/fix step inside workspace_dir."""
        raise NotImplementedError
