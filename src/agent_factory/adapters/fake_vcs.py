"""Scripted fake VCS and CI adapter for offline deterministic testing (INV-02, INV-03, INV-15)."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agent_factory.ports.vcs import CIRunResult


class FakeVCS:
    """Fake VCS adapter recording pushes, CI checks, and merges in memory."""

    def __init__(
        self,
        ci_results: Sequence[CIRunResult | Exception] | None = None,
        repo_base_url: str = "https://github.com/agent-factory-test",
        deploy_base_url: str = "https://agent-factory-test.github.io",
    ) -> None:
        self._ci_results: list[CIRunResult | Exception] = list(ci_results or [])
        self.repo_base_url = repo_base_url.rstrip("/")
        self.deploy_base_url = deploy_base_url.rstrip("/")
        self.pushes: list[dict[str, Any]] = []
        self.ci_checks: list[dict[str, Any]] = []
        self.merges: list[dict[str, Any]] = []

    def enqueue_ci_result(self, result: CIRunResult | Exception) -> None:
        """Append a scripted CIRunResult or Exception to the queue."""
        raise NotImplementedError

    def push_branch(
        self,
        project_id: str,
        workspace_dir: Path,
        branch: str = "work",
    ) -> str:
        """Record a branch push and return a deterministic fake commit ref."""
        raise NotImplementedError

    def get_ci_result(
        self,
        project_id: str,
        branch: str = "work",
    ) -> CIRunResult:
        """Consume and return the next scripted CIRunResult."""
        raise NotImplementedError

    def merge_and_deploy(
        self,
        project_id: str,
        branch: str = "work",
    ) -> tuple[str, str]:
        """Record merge/deploy and return (repo_url, deploy_url)."""
        raise NotImplementedError
