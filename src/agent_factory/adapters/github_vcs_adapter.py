"""Real GitHub VCS and CI adapter delegating commands to Runner with clean env (INV-02, INV-07, INV-19, INV-22)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.ports.runner import Runner
from agent_factory.ports.vcs import VCS, CIRunResult

if TYPE_CHECKING:
    from agent_factory.adapters.static_deploy_publisher import StaticDeployPublisher


class VcsError(RuntimeError):
    """Raised when a Git/GitHub operation fails, with all secrets redacted (INV-07)."""


class GitHubVCSAdapter(VCS):
    """Executes git/gh CLI workflows over workspaces via Runner with clean environment."""

    def __init__(
        self,
        runner: Runner | None = None,
        *,
        owner: str = "agent-factory",
        repo_base_url: str = "https://github.com",
        deploy_base_url: str = "",
        default_branch: str = "main",
        github_token: str = "",
        secrets: Sequence[str] = (),
        deploy_publisher: StaticDeployPublisher | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.runner: Runner = (
            runner
            if runner is not None
            else SubprocessRunner(default_timeout_seconds=timeout_seconds)
        )
        self.owner = owner
        self.repo_base_url = repo_base_url.rstrip("/")
        self.deploy_base_url = (
            deploy_base_url.rstrip("/")
            if deploy_base_url
            else f"https://{owner}.github.io"
        )
        self.default_branch = default_branch
        self.github_token = github_token
        self.secrets: tuple[str, ...] = tuple(secrets)
        self.deploy_publisher = deploy_publisher
        self.timeout_seconds = timeout_seconds

    def build_clean_env(self, host_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """Build sanitized subprocess env keeping system vars and stripping unrelated secrets (INV-19)."""
        raise NotImplementedError("GitHubVCSAdapter.build_clean_env is not implemented yet (PASO B)")

    def push_branch(
        self,
        project_id: str,
        workspace_dir: Path,
        branch: str = "work",
    ) -> str:
        """Validate project_id/branch and push workspace branch via Runner."""
        raise NotImplementedError("GitHubVCSAdapter.push_branch is not implemented yet (PASO B)")

    def get_ci_result(
        self,
        project_id: str,
        branch: str = "work",
    ) -> CIRunResult:
        """Query remote CI workflow result for project_id and branch via Runner."""
        raise NotImplementedError("GitHubVCSAdapter.get_ci_result is not implemented yet (PASO B)")

    def merge_and_deploy(
        self,
        project_id: str,
        branch: str = "work",
    ) -> tuple[str, str]:
        """Merge verified branch and deploy static artifact, returning (repo_url, deploy_url)."""
        raise NotImplementedError("GitHubVCSAdapter.merge_and_deploy is not implemented yet (PASO B)")
