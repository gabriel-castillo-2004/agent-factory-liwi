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
        self._test_gen_steps.append(step)

    def enqueue_impl(self, step: ImplStep) -> None:
        """Append a scripted implementation/fix step."""
        self._impl_steps.append(step)

    def generate_tests(
        self,
        workspace_dir: Path,
        acceptance_tests: Sequence[AcceptanceTest],
    ) -> CodeAgentResult:
        """Execute next scripted test-generation step using ONLY acceptance_tests (A2a-5)."""
        seen_files: list[str] = []
        src_dir = workspace_dir / "src"
        if src_dir.is_dir():
            seen_files = sorted(
                str(p.relative_to(workspace_dir).as_posix())
                for p in src_dir.rglob("*")
                if p.is_file()
            )
        self.test_gen_calls.append(
            {
                "workspace_dir": Path(workspace_dir),
                "acceptance_tests": list(acceptance_tests),
                "production_files_seen": seen_files,
            }
        )
        if not self._test_gen_steps:
            return CodeAgentResult(files_written=[], summary="No scripted test gen step")
        step = self._test_gen_steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, type) and issubclass(step, Exception):
            raise step()
        if callable(step):
            return step(workspace_dir, acceptance_tests)
        if isinstance(step, dict):
            files_written: list[str] = []
            for rel_path, content in step.items():
                p = workspace_dir / rel_path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                files_written.append(rel_path)
            return CodeAgentResult(
                files_written=files_written,
                summary="Scripted test gen step executed",
            )
        return CodeAgentResult(files_written=[], summary="Unknown test gen step")

    def implement_or_fix(
        self,
        workspace_dir: Path,
        plan: Plan,
        failure_output: str = "",
        attempt: int = 1,
    ) -> CodeAgentResult:
        """Execute next scripted implementation/fix step inside workspace_dir."""
        self.impl_calls.append(
            {
                "workspace_dir": Path(workspace_dir),
                "plan": plan,
                "failure_output": failure_output,
                "attempt": attempt,
            }
        )
        if not self._impl_steps:
            return CodeAgentResult(files_written=[], summary="No scripted impl step")
        step = self._impl_steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, type) and issubclass(step, Exception):
            raise step()
        if callable(step):
            return step(workspace_dir, plan, failure_output, attempt)
        if isinstance(step, dict):
            files_written: list[str] = []
            for rel_path, content in step.items():
                p = workspace_dir / rel_path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                files_written.append(rel_path)
            return CodeAgentResult(
                files_written=files_written,
                summary="Scripted impl step executed",
            )
        return CodeAgentResult(files_written=[], summary="Unknown impl step")
