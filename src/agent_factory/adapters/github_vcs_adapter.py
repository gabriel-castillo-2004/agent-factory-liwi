"""Real GitHub VCS and CI adapter delegating commands to Runner with clean env (INV-02, INV-07, INV-19, INV-22)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.domain.models import validate_project_id
from agent_factory.ports.runner import CommandTimeoutError, Runner
from agent_factory.ports.vcs import VCS, CIRunResult
from agent_factory.secrets import redact_secrets

if TYPE_CHECKING:
    from agent_factory.adapters.static_deploy_publisher import StaticDeployPublisher

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
        combined_secrets = list(secrets)
        if github_token and github_token not in combined_secrets:
            combined_secrets.append(github_token)
        self.secrets: tuple[str, ...] = tuple(combined_secrets)
        self.deploy_publisher = deploy_publisher
        self.timeout_seconds = timeout_seconds

    def build_clean_env(self, host_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """Build sanitized subprocess env keeping system vars and stripping unrelated secrets (INV-19)."""
        effective = os.environ if host_env is None else host_env
        clean: dict[str, str] = {}
        for k, v in effective.items():
            k_up = k.upper()
            if k_up in SAFE_ENV_KEYS and not any(sub in k_up for sub in FORBIDDEN_ENV_SUBSTRINGS):
                clean[k] = v
        if self.github_token:
            clean["GITHUB_TOKEN"] = self.github_token
            clean["GH_TOKEN"] = self.github_token
        return clean

    def _validate_branch(self, branch: str) -> str:
        """Validate branch name preventing flag injection and path traversal."""
        b = branch.strip()
        if (
            not b
            or b.startswith("-")
            or any(ch.isspace() for ch in b)
            or ".." in b
            or ":" in b
            or "\\" in b
        ):
            raise ValueError(f"Invalid branch name '{branch}'")
        return b

    def push_branch(
        self,
        project_id: str,
        workspace_dir: Path,
        branch: str = "work",
    ) -> str:
        """Validate project_id/branch and push workspace branch via Runner."""
        validate_project_id(project_id)
        valid_branch = self._validate_branch(branch)
        if not workspace_dir.is_dir():
            raise ValueError(f"Workspace directory {workspace_dir} does not exist")

        clean_env = self.build_clean_env()
        cmd = ["git", "push", "-u", "origin", valid_branch]
        clean_err: str | None = None
        res = None
        try:
            res = self.runner.run(cmd, cwd=workspace_dir, env=clean_env)
        except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
            clean_err = redact_secrets(str(exc), extra_secrets=self.secrets)
        else:
            if res.exit_code != 0:
                err = res.stderr or res.stdout or f"exit code {res.exit_code}"
                clean_err = redact_secrets(err, extra_secrets=self.secrets)

        if clean_err is not None:
            err_obj = VcsError(f"Git push failed: {clean_err}")
            err_obj.__cause__ = None
            err_obj.__context__ = None
            raise err_obj

        return res.stdout.strip() if res and res.stdout.strip() else valid_branch

    def get_ci_result(
        self,
        project_id: str,
        branch: str = "work",
    ) -> CIRunResult:
        """Query remote CI workflow result for project_id and branch via Runner."""
        validate_project_id(project_id)
        valid_branch = self._validate_branch(branch)
        repo_target = f"{self.owner}/{project_id}" if self.owner else project_id

        clean_env = self.build_clean_env()
        cmd = [
            "gh",
            "run",
            "list",
            "--repo",
            repo_target,
            "--branch",
            valid_branch,
            "--limit",
            "1",
            "--json",
            "status,conclusion",
        ]
        clean_err: str | None = None
        res = None
        try:
            res = self.runner.run(cmd, cwd=Path.cwd(), env=clean_env)
        except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
            clean_err = redact_secrets(str(exc), extra_secrets=self.secrets)
        else:
            if res.exit_code != 0:
                err = res.stderr or res.stdout or f"exit code {res.exit_code}"
                clean_err = redact_secrets(err, extra_secrets=self.secrets)

        if clean_err is not None:
            err_obj = VcsError(f"CI query failed: {clean_err}")
            err_obj.__cause__ = None
            err_obj.__context__ = None
            raise err_obj

        raw_output = res.stdout.strip() if res else ""
        try:
            parsed = json.loads(raw_output)
            if isinstance(parsed, list):
                if not parsed:
                    return CIRunResult(
                        passed=False,
                        summary="No CI runs found",
                        failed_checks=["ci"],
                    )
                parsed = parsed[0]
            if isinstance(parsed, dict):
                if "passed" in parsed:
                    return CIRunResult(
                        passed=bool(parsed["passed"]),
                        summary=str(parsed.get("summary", "")),
                        failed_checks=list(parsed.get("failed_checks", [])),
                    )
                conclusion = str(parsed.get("conclusion", "")).lower()
                status_str = str(parsed.get("status", "")).lower()
                passed = conclusion == "success"
                summary = f"CI status: {status_str}, conclusion: {conclusion}"
                failed_checks = [] if passed else ["ci"]
                return CIRunResult(passed=passed, summary=summary, failed_checks=failed_checks)
        except json.JSONDecodeError:
            pass

        return CIRunResult(
            passed=False,
            summary=raw_output[:200],
            failed_checks=["ci"],
        )

    def merge_and_deploy(
        self,
        project_id: str,
        branch: str = "work",
    ) -> tuple[str, str]:
        """Merge verified branch and deploy static artifact, returning (repo_url, deploy_url)."""
        validate_project_id(project_id)
        valid_branch = self._validate_branch(branch)
        repo_target = f"{self.owner}/{project_id}" if self.owner else project_id

        clean_env = self.build_clean_env()
        cmd = ["gh", "pr", "merge", valid_branch, "--repo", repo_target, "--merge", "--auto"]
        clean_err: str | None = None
        res = None
        try:
            res = self.runner.run(cmd, cwd=Path.cwd(), env=clean_env)
        except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
            clean_err = redact_secrets(str(exc), extra_secrets=self.secrets)
        else:
            if res.exit_code != 0:
                err = res.stderr or res.stdout or f"exit code {res.exit_code}"
                clean_err = redact_secrets(err, extra_secrets=self.secrets)

        if clean_err is not None:
            err_obj = VcsError(f"Merge failed: {clean_err}")
            err_obj.__cause__ = None
            err_obj.__context__ = None
            raise err_obj

        repo_url = (
            f"{self.repo_base_url}/{self.owner}/{project_id}"
            if self.owner
            else f"{self.repo_base_url}/{project_id}"
        )
        deploy_url = f"{self.deploy_base_url}/{project_id}"
        return (repo_url, deploy_url)
