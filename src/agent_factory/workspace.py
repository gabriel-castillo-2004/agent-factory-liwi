"""Isolated workspace sandbox and deterministic test-lock hash verification (INV-13, INV-18, INV-22)."""

from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, Field

RUNNER_CONFIG_FILENAMES: tuple[str, ...] = (
    "pyproject.toml",
    "pytest.ini",
    "package.json",
    "vitest.config.ts",
    "playwright.config.ts",
)


class SandboxViolationError(ValueError):
    """Raised when a path or project_id attempts to escape workspaces/<project_id>/ (INV-18, INV-22)."""


class TestLockSnapshot(BaseModel):
    """Cryptographic SHA-256 lock of tests/ and runner configuration files (INV-13, AC-2.2)."""

    tests_hash: str
    runner_config_hash: str
    combined_hash: str
    saved_files: dict[str, str] = Field(default_factory=dict)


class WorkspaceManager:
    """Manages isolated workspaces/<project_id>/ directories and enforces path containment."""

    def __init__(self, base_dir: Path = Path("workspaces")) -> None:
        self.base_dir = Path(base_dir).resolve()

    def get_workspace_dir(self, project_id: str) -> Path:
        """Validate project_id and return the resolved workspace directory path."""
        raise NotImplementedError

    def create_workspace(
        self,
        project_id: str,
        template_files: Mapping[str, str] | None = None,
    ) -> Path:
        """Create an isolated workspace for project_id and populate optional initial files."""
        raise NotImplementedError

    def resolve_safe_path(self, project_id: str, relative_path: str | Path) -> Path:
        """Resolve relative_path inside workspaces/<project_id>/ and reject any traversal or symlink escape."""
        raise NotImplementedError

    def write_file(self, project_id: str, relative_path: str | Path, content: str) -> Path:
        """Safely write content to relative_path inside workspaces/<project_id>/."""
        raise NotImplementedError

    def read_file(self, project_id: str, relative_path: str | Path) -> str:
        """Safely read content from relative_path inside workspaces/<project_id>/."""
        raise NotImplementedError


def compute_test_lock(
    workspace_dir: Path,
    runner_config_names: Sequence[str] = RUNNER_CONFIG_FILENAMES,
) -> TestLockSnapshot:
    """Compute deterministic SHA-256 hashes and snapshot contents for tests/ and runner configs (INV-13)."""
    raise NotImplementedError


def verify_and_restore_test_lock(
    workspace_dir: Path,
    snapshot: TestLockSnapshot,
    runner_config_names: Sequence[str] = RUNNER_CONFIG_FILENAMES,
) -> bool:
    """Verify tests/ and runner config hashes against snapshot; if altered, revert files and return False."""
    raise NotImplementedError
