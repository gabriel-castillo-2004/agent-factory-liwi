"""Static site and PWA panel deployment adapter delegating via Runner with clean env (INV-02, INV-07, INV-19, INV-22)."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.domain.events import Event
from agent_factory.domain.models import BuildReport, ProjectStatus, validate_project_id
from agent_factory.ports.runner import CommandTimeoutError, Runner
from agent_factory.secrets import redact_secrets

SAFE_ENV_KEYS: frozenset[str] = frozenset(
    {
        "PATH",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "WINDIR",
        "TMP",
        "TEMP",
        "USER",
        "USERNAME",
        "HOME",
        "USERPROFILE",
        "LANG",
        "LC_ALL",
        "TERM",
        "GIT_EXEC_PATH",
    }
)
FORBIDDEN_ENV_SUBSTRINGS: tuple[str, ...] = (
    "TELEGRAM",
    "LLM",
    "OPENAI",
    "ANTHROPIC",
    "GEMINI",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
)


def _sanitize_data(data: Any, secrets: Sequence[str]) -> Any:
    """Recursively scrub secrets from nested dicts, lists, and strings without corrupting JSON."""
    if isinstance(data, str):
        return redact_secrets(data, extra_secrets=secrets)
    if isinstance(data, dict):
        return {k: _sanitize_data(v, secrets) for k, v in data.items()}
    if isinstance(data, list):
        return [_sanitize_data(item, secrets) for item in data]
    return data


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
        combined_secrets = list(secrets)
        if deploy_token and deploy_token not in combined_secrets:
            combined_secrets.append(deploy_token)
        self.secrets: tuple[str, ...] = tuple(combined_secrets)
        self.timeout_seconds = timeout_seconds

    def build_clean_env(self, host_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """Construct sanitized subprocess env excluding unrelated host secrets (INV-19)."""
        effective = os.environ if host_env is None else host_env
        clean: dict[str, str] = {}
        for k, v in effective.items():
            k_up = k.upper()
            if k_up in SAFE_ENV_KEYS and not any(sub in k_up for sub in FORBIDDEN_ENV_SUBSTRINGS):
                clean[k] = v
        if self.deploy_token:
            clean["VERCEL_TOKEN"] = self.deploy_token
            clean["GITHUB_TOKEN"] = self.deploy_token
        return clean

    def build_sanitized_project_detail(
        self,
        status: ProjectStatus,
        events: Sequence[Event] = (),
        *,
        build_report: BuildReport | None = None,
        project_prompt: str | None = None,
    ) -> dict[str, Any]:
        """Build sanitized JSON-serializable detail dict for projects/<project_id>.json."""
        validate_project_id(status.project_id)
        state_str = (
            status.state.value if hasattr(status.state, "value") else str(status.state)
        )
        detail: dict[str, Any] = {
            "project_id": status.project_id,
            "name": status.name,
            "objective": status.objective,
            "state": state_str,
            "phase": status.phase,
            "progress_pct": status.progress_pct,
            "plan_hash": status.plan_hash,
            "tests": status.tests.model_dump() if status.tests else None,
            "agent_invocations": status.agent_invocations,
            "ci_rounds": status.ci_rounds,
            "links": status.links.model_dump() if status.links else None,
            "updated_at": status.updated_at.isoformat() if status.updated_at else None,
            "events": [
                {
                    "id": ev.id,
                    "type": ev.type.value if hasattr(ev.type, "value") else str(ev.type),
                    "payload": ev.payload,
                    "at": ev.at.isoformat() if ev.at else None,
                }
                for ev in events
            ],
            "build_report": build_report.model_dump() if build_report else None,
            "project_prompt": project_prompt,
        }
        return _sanitize_data(detail, self.secrets)

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
        for s in statuses:
            validate_project_id(s.project_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        if panel_source_dir.is_dir():
            shutil.copytree(panel_source_dir, output_dir, dirs_exist_ok=True)

        projects_dir = output_dir / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)

        reg_projects: dict[str, Any] = {}
        for s in statuses:
            state_str = s.state.value if hasattr(s.state, "value") else str(s.state)
            repo_url = s.links.repo if s.links else None
            deploy_url = s.links.deploy if s.links else None
            raw_entry = {
                "project_id": s.project_id,
                "name": s.name,
                "objective": s.objective,
                "state": state_str,
                "progress_pct": s.progress_pct,
                "plan_hash": s.plan_hash,
                "repo_url": repo_url,
                "deploy_url": deploy_url,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            }
            reg_projects[s.project_id] = _sanitize_data(raw_entry, self.secrets)
            evs = (events_by_project or {}).get(s.project_id, ())
            rep = (reports_by_project or {}).get(s.project_id)
            detail = self.build_sanitized_project_detail(s, events=evs, build_report=rep)
            (projects_dir / f"{s.project_id}.json").write_text(
                json.dumps(detail, indent=2, sort_keys=True), encoding="utf-8"
            )

        reg_data = {
            "version": "1.0",
            "updated_at": datetime.now(UTC).isoformat(),
            "projects": reg_projects,
        }
        sanitized_reg_data = _sanitize_data(reg_data, self.secrets)
        (output_dir / "registry.json").write_text(
            json.dumps(sanitized_reg_data, indent=2, sort_keys=True), encoding="utf-8"
        )
        return output_dir

    def deploy_site(
        self,
        project_id: str,
        source_dir: Path,
        *,
        branch: str | None = None,
    ) -> str:
        """Validate project_id/source_dir and deploy static artifact via Runner, returning deploy URL."""
        validate_project_id(project_id)
        resolved_source = Path(source_dir).resolve()
        if not resolved_source.is_dir():
            raise ValueError(f"Source directory {source_dir} does not exist")

        chosen_branch = self.branch if branch is None else branch
        effective_branch = chosen_branch.strip()
        if (
            not effective_branch
            or effective_branch.startswith("-")
            or any(ch.isspace() for ch in effective_branch)
            or ".." in effective_branch
            or effective_branch in {"main", "master", "refs/heads/main", "refs/heads/master"}
        ):
            raise ValueError(f"Invalid or protected branch: '{effective_branch}'")

        clean_env = self.build_clean_env()
        cmd = ["gh", "pages", "deploy", str(resolved_source), "--branch", effective_branch]
        clean_err: str | None = None
        try:
            res = self.runner.run(cmd, cwd=resolved_source, env=clean_env)
        except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
            clean_err = redact_secrets(str(exc), extra_secrets=self.secrets)
        else:
            if res.exit_code != 0:
                err = res.stderr or res.stdout or f"exit code {res.exit_code}"
                clean_err = redact_secrets(err, extra_secrets=self.secrets)

        if clean_err is not None:
            err_obj = StaticDeployError(f"Static deploy failed: {clean_err}")
            err_obj.__cause__ = None
            err_obj.__context__ = None
            raise err_obj

        return f"{self.deploy_base_url}/{project_id}"
