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
        self._ci_results.append(result)

    def push_branch(
        self,
        project_id: str,
        workspace_dir: Path,
        branch: str = "work",
    ) -> str:
        """Record a branch push and return a deterministic fake commit ref."""
        push_ref = f"ref-push-{len(self.pushes) + 1}"
        self.pushes.append(
            {
                "project_id": project_id,
                "workspace_dir": Path(workspace_dir),
                "branch": branch,
                "ref": push_ref,
            }
        )
        return push_ref

    def get_ci_result(
        self,
        project_id: str,
        branch: str = "work",
    ) -> CIRunResult:
        """Consume and return the next scripted CIRunResult."""
        self.ci_checks.append({"project_id": project_id, "branch": branch})
        if not self._ci_results:
            return CIRunResult(passed=True, summary="CI green")
        res = self._ci_results.pop(0)
        if isinstance(res, Exception):
            raise res
        if isinstance(res, type) and issubclass(res, Exception):
            raise res()
        return res

    def merge_and_deploy(
        self,
        project_id: str,
        branch: str = "work",
    ) -> tuple[str, str]:
        """Record merge/deploy and return (repo_url, deploy_url)."""
        repo_url = f"{self.repo_base_url}/{project_id}"
        deploy_url = f"{self.deploy_base_url}/{project_id}"
        self.merges.append(
            {
                "project_id": project_id,
                "branch": branch,
                "repo_url": repo_url,
                "deploy_url": deploy_url,
            }
        )
        return repo_url, deploy_url
