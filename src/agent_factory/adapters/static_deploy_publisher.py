"""Static site and PWA panel deployment adapter delegating via Runner with clean env (INV-02, INV-07, INV-19, INV-22)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.domain.events import Event
from agent_factory.domain.models import BuildReport, ProjectStatus
from agent_factory.ports.runner import Runner


class StaticDeployError(RuntimeError):
    """Raised when static deployment fails, with sensitive tokens redacted (INV-07)."""


class StaticDeployPublisher:
    """Deploys static build outputs or panel bundles onto free static hosting via Runner."""

    def __init__(
        self,
        runner: Runner | None = None,
        *,
        target: str = "github-pages",
        deploy_base_url: str = "https://agent-factory.github.io",
        branch: str = "gh-pages",
        deploy_token: str = "",
        secrets: Sequence[str] = (),
        timeout_seconds: float = 60.0,
    ) -> None:
        self.runner: Runner = (
            runner
            if runner is not None
            else SubprocessRunner(default_timeout_seconds=timeout_seconds)
        )
        self.target = target
        self.deploy_base_url = deploy_base_url.rstrip("/")
        self.branch = branch
        self.deploy_token = deploy_token
        self.secrets: tuple[str, ...] = tuple(secrets)
        self.timeout_seconds = timeout_seconds

    def build_clean_env(self, host_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """Construct sanitized subprocess env excluding unrelated host secrets (INV-19)."""
        raise NotImplementedError("StaticDeployPublisher.build_clean_env is not implemented yet (PASO B)")

    def export_panel_bundle(
        self,
        output_dir: Path,
        statuses: Sequence[ProjectStatus],
        *,
        events_by_project: Mapping[str, Sequence[Event]] | None = None,
        reports_by_project: Mapping[str, BuildReport] | None = None,
        panel_source_dir: Path = Path("panel"),
    ) -> Path:
        """Assemble static PWA panel assets with sanitized registry.json and projects/<id>.json."""
        raise NotImplementedError("StaticDeployPublisher.export_panel_bundle is not implemented yet (PASO B)")

    def build_sanitized_project_detail(
        self,
        status: ProjectStatus,
        events: Sequence[Event] = (),
        *,
        build_report: BuildReport | None = None,
        project_prompt: str | None = None,
    ) -> dict[str, Any]:
        """Build sanitized JSON-serializable detail dict for projects/<project_id>.json."""
        raise NotImplementedError("StaticDeployPublisher.build_sanitized_project_detail is not implemented yet (PASO B)")

    def deploy_site(
        self,
        project_id: str,
        source_dir: Path,
        *,
        branch: str | None = None,
    ) -> str:
        """Validate project_id/source_dir and deploy static artifact via Runner, returning deploy URL."""
        raise NotImplementedError("StaticDeployPublisher.deploy_site is not implemented yet (PASO B)")
