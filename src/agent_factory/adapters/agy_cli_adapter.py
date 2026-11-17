"""Real CLI code agent adapter for local execution inside workspaces/<project_id>/ (INV-06, INV-09, INV-18, INV-19, ADR-002)."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.domain.models import AcceptanceTest, Plan
from agent_factory.ports.code_agent import CodeAgent, CodeAgentResult
from agent_factory.ports.runner import Runner
from agent_factory.workspace import WorkspaceManager

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
        raise NotImplementedError

    def build_clean_env(
        self,
        host_env: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Construct a sanitized environment dictionary keeping only whitelisted variables and stripping host secrets (INV-19)."""
        raise NotImplementedError

    def build_test_generation_payload(
        self,
        workspace_dir: Path,
        acceptance_tests: Sequence[AcceptanceTest],
    ) -> dict[str, Any]:
        """Build isolated payload for generate_tests containing only acceptance_tests without Plan or src/ (A2a-5, A2b-4)."""
        raise NotImplementedError

    def build_implementation_payload(
        self,
        workspace_dir: Path,
        plan: Plan,
        failure_output: str = "",
        attempt: int = 1,
    ) -> dict[str, Any]:
        """Build payload for implement_or_fix containing Plan, failure_output, and attempt number (A2b-4)."""
        raise NotImplementedError

    def generate_tests(
        self,
        workspace_dir: Path,
        acceptance_tests: Sequence[AcceptanceTest],
    ) -> CodeAgentResult:
        """Execute CLI in generate-tests mode inside workspace_dir using only acceptance_tests (INV-13, A2b-4)."""
        raise NotImplementedError

    def implement_or_fix(
        self,
        workspace_dir: Path,
        plan: Plan,
        failure_output: str = "",
        attempt: int = 1,
    ) -> CodeAgentResult:
        """Execute CLI in implement mode inside workspace_dir with Plan and failure report (A2b-4, A2b-5)."""
        raise NotImplementedError
