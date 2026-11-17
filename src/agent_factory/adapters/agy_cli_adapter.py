"""Real CLI code agent adapter for local execution inside workspaces/<project_id>/ (INV-06, INV-09, INV-18, INV-19, ADR-002)."""

import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.domain.models import AcceptanceTest, Plan
from agent_factory.ports.code_agent import (
    CodeAgent,
    CodeAgentError,
    CodeAgentResult,
    QuotaExhaustedError,
)
from agent_factory.ports.runner import Runner
from agent_factory.workspace import SandboxViolationError, WorkspaceManager

ALLOWED_SYSTEM_ENV_VARS: frozenset[str] = frozenset(
    {
        "PATH",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "WINDIR",
        "HOME",
        "USERPROFILE",
        "TMP",
        "TEMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
    }
)

FORBIDDEN_SECRET_ENV_SUBSTRINGS: tuple[str, ...] = (
    "TOKEN",
    "SECRET",
    "API_KEY",
    "PASSWORD",
    "CREDENTIAL",
    "PRIVATE_KEY",
)


class AgyCliAdapter(CodeAgent):
    """Executes the local CLI code agent inside an isolated workspace with a clean environment."""

    def __init__(
        self,
        workspace_manager: WorkspaceManager,
        runner: Runner | None = None,
        cli_command: Sequence[str] = ("agy",),
        timeout_seconds: float = 60.0,
        allowed_cli_env_keys: Sequence[str] = ("AGY_HOME", "AGY_CONFIG_DIR"),
        default_retry_after_seconds: float = 5.0,
    ) -> None:
        self.workspace_manager = workspace_manager
        self.runner: Runner = (
            runner
            if runner is not None
            else SubprocessRunner(default_timeout_seconds=timeout_seconds)
        )
        self.cli_command: tuple[str, ...] = tuple(cli_command)
        self.timeout_seconds = timeout_seconds
        self.allowed_cli_env_keys: frozenset[str] = frozenset(allowed_cli_env_keys)
        self.default_retry_after_seconds = default_retry_after_seconds

    def validate_workspace_cwd(self, workspace_dir: Path) -> Path:
        """Verify workspace_dir is a valid isolated workspace directory under workspace_manager.base_dir (INV-18, INV-22)."""
        ws_path = Path(workspace_dir).resolve()
        base_dir = self.workspace_manager.base_dir.resolve()
        if not ws_path.is_relative_to(base_dir) or ws_path == base_dir:
            raise SandboxViolationError(
                f"Workspace cwd '{workspace_dir}' is not inside workspace base '{base_dir}'"
            )
        if ws_path.parent != base_dir:
            raise SandboxViolationError(
                f"Workspace cwd '{workspace_dir}' is a nested subdirectory under '{base_dir}'"
            )
        project_id = ws_path.name
        try:
            self.workspace_manager.get_workspace_dir(project_id)
        except (ValueError, SandboxViolationError) as err:
            raise SandboxViolationError(
                f"Invalid workspace directory name '{project_id}': {err}"
            ) from err
        return ws_path

    def build_clean_env(
        self,
        host_env: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Construct a sanitized environment dictionary keeping only whitelisted variables and stripping host secrets (INV-19)."""
        source = os.environ if host_env is None else host_env
        clean_env: dict[str, str] = {}
        allowed_sys_upper = {k.upper() for k in ALLOWED_SYSTEM_ENV_VARS}
        allowed_cli_upper = {k.upper() for k in self.allowed_cli_env_keys}

        for k, v in source.items():
            k_upper = k.upper()
            if any(sub in k_upper for sub in FORBIDDEN_SECRET_ENV_SUBSTRINGS):
                continue
            if k_upper in allowed_sys_upper:
                canonical_k = "PATH" if k_upper == "PATH" else k
                clean_env[canonical_k] = v
            elif k_upper in allowed_cli_upper or k in self.allowed_cli_env_keys:
                clean_env[k] = v
        return clean_env

    def build_test_generation_payload(
        self,
        workspace_dir: Path,
        acceptance_tests: Sequence[AcceptanceTest],
    ) -> dict[str, Any]:
        """Build isolated payload for generate_tests containing only acceptance_tests without Plan or src/ (A2a-5, A2b-4)."""
        ws_path = self.validate_workspace_cwd(workspace_dir)
        src_dir = ws_path / "src"
        if src_dir.is_dir() and any(p.is_file() for p in src_dir.rglob("*")):
            raise SandboxViolationError(
                f"Production files already exist in src/ before test generation: {ws_path}"
            )
        return {
            "mode": "generate_tests",
            "workspace_dir": str(ws_path),
            "acceptance_tests": [
                {
                    "id": at.id,
                    "description": at.description,
                    "type": at.type.value if hasattr(at.type, "value") else str(at.type),
                }
                for at in acceptance_tests
            ],
        }

    def build_implementation_payload(
        self,
        workspace_dir: Path,
        plan: Plan,
        failure_output: str = "",
        attempt: int = 1,
    ) -> dict[str, Any]:
        """Build payload for implement_or_fix containing Plan, failure_output, and attempt number (A2b-4)."""
        ws_path = self.validate_workspace_cwd(workspace_dir)
        return {
            "mode": "implement_or_fix",
            "workspace_dir": str(ws_path),
            "attempt": attempt,
            "failure_output": failure_output,
            "plan": plan.model_dump(mode="json"),
        }

    def _run_cli(
        self,
        ws_path: Path,
        mode_arg: str,
        payload: dict[str, Any],
    ) -> CodeAgentResult:
        cmd = list(self.cli_command) + [
            "--mode",
            mode_arg,
            "--payload",
            json.dumps(payload),
        ]
        clean_env = self.build_clean_env()
        res = self.runner.run(
            command=cmd,
            cwd=ws_path,
            timeout_seconds=self.timeout_seconds,
            env=clean_env,
        )

        combined_output = f"{res.stdout}\n{res.stderr}".strip()

        # Handle non-zero exit codes (quota exhaustion or fatal agent failures)
        if res.exit_code != 0:
            lowered = combined_output.lower()
            quota_phrases = (
                "too many requests",
                "quota exhausted",
                "quota exceeded",
                "rate limit",
                "rate_limit",
                "resource_exhausted",
                "insufficient quota",
            )
            is_quota = (
                res.exit_code in {42, 429}
                or any(phrase in lowered for phrase in quota_phrases)
                or bool(
                    re.search(
                        r"\b(?:http|status|error|code)\s*[:=]?\s*429\b",
                        combined_output,
                        re.IGNORECASE,
                    )
                )
            )

            if is_quota:
                retry_match = re.search(
                    r"retry_after[ =:]+([0-9.]+)", combined_output, re.IGNORECASE
                )
                retry_after = (
                    float(retry_match.group(1))
                    if retry_match
                    else self.default_retry_after_seconds
                )
                raise QuotaExhaustedError(
                    message=combined_output or "CLI quota exhausted",
                    retry_after_seconds=retry_after,
                )

            raise CodeAgentError(
                f"CLI execution failed with exit code {res.exit_code}: {combined_output}"
            )

        # Parse success output
        files_written: list[str] = []
        summary = ""
        output_text = res.stdout.strip()
        if output_text:
            try:
                try:
                    data = json.loads(output_text)
                    if isinstance(data, dict):
                        files_written = data.get("files_written", [])
                        summary = data.get("summary", "")
                    else:
                        summary = output_text
                except (json.JSONDecodeError, ValueError):
                    for line in output_text.splitlines()[::-1]:
                        line_s = line.strip()
                        if line_s.startswith("{") and line_s.endswith("}"):
                            data = json.loads(line_s)
                            if isinstance(data, dict):
                                files_written = data.get("files_written", [])
                                summary = data.get("summary", "")
                                break
                    else:
                        summary = output_text
            except (json.JSONDecodeError, ValueError, TypeError):
                summary = output_text

        return CodeAgentResult(files_written=files_written, summary=summary)

    def generate_tests(
        self,
        workspace_dir: Path,
        acceptance_tests: Sequence[AcceptanceTest],
    ) -> CodeAgentResult:
        """Execute CLI in generate-tests mode inside workspace_dir using only acceptance_tests (INV-13, A2b-4)."""
        ws_path = self.validate_workspace_cwd(workspace_dir)
        payload = self.build_test_generation_payload(ws_path, acceptance_tests)
        return self._run_cli(ws_path, "generate-tests", payload)

    def implement_or_fix(
        self,
        workspace_dir: Path,
        plan: Plan,
        failure_output: str = "",
        attempt: int = 1,
    ) -> CodeAgentResult:
        """Execute CLI in implement mode inside workspace_dir with Plan and failure report (A2b-4, A2b-5)."""
        ws_path = self.validate_workspace_cwd(workspace_dir)
        payload = self.build_implementation_payload(
            ws_path, plan, failure_output=failure_output, attempt=attempt
        )
        return self._run_cli(ws_path, "implement", payload)
