"""Isolated workspace sandbox and deterministic test-lock hash verification (INV-13, INV-18, INV-22)."""

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from agent_factory.domain.models import validate_project_id

RUNNER_CONFIG_FILENAMES: tuple[str, ...] = (
    "pyproject.toml",
    "pytest.ini",
    "package.json",
    "vitest.config.ts",
    "playwright.config.ts",
    "conftest.py",
    "setup.cfg",
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
        try:
            validate_project_id(project_id)
        except ValueError as err:
            raise SandboxViolationError(str(err)) from err

        target = (self.base_dir / project_id).resolve()
        if not target.is_relative_to(self.base_dir) or target == self.base_dir:
            raise SandboxViolationError(f"Escape attempt via project_id: {project_id}")
        return target

    def create_workspace(
        self,
        project_id: str,
        template_files: Mapping[str, str] | None = None,
    ) -> Path:
        """Create an isolated workspace for project_id and populate optional initial files."""
        ws_dir = self.get_workspace_dir(project_id)
        ws_dir.mkdir(parents=True, exist_ok=True)
        if template_files:
            for rel_path, content in template_files.items():
                self.write_file(project_id, rel_path, content)
        return ws_dir

    def resolve_safe_path(self, project_id: str, relative_path: str | Path) -> Path:
        """Resolve relative_path inside workspaces/<project_id>/ and reject any traversal or symlink escape."""
        ws_dir = self.get_workspace_dir(project_id)
        path_str = str(relative_path).strip()
        if not path_str:
            raise SandboxViolationError("Empty relative path is invalid")

        if (
            path_str.startswith(("/", "\\"))
            or Path(relative_path).is_absolute()
            or (len(path_str) > 1 and path_str[1] == ":")
        ):
            raise SandboxViolationError(
                f"Absolute path or drive letter rejected: {relative_path}"
            )

        candidate = (ws_dir / relative_path).resolve()
        if not candidate.is_relative_to(ws_dir) or candidate == ws_dir:
            raise SandboxViolationError(
                f"Path '{relative_path}' resolves outside workspace '{ws_dir}': {candidate}"
            )
        return candidate

    def write_file(self, project_id: str, relative_path: str | Path, content: str) -> Path:
        """Safely write content to relative_path inside workspaces/<project_id>/."""
        target_path = self.resolve_safe_path(project_id, relative_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")
        return target_path

    def read_file(self, project_id: str, relative_path: str | Path) -> str:
        """Safely read content from relative_path inside workspaces/<project_id>/."""
        target_path = self.resolve_safe_path(project_id, relative_path)
        if not target_path.is_file():
            raise FileNotFoundError(f"File not found: {target_path}")
        return target_path.read_text(encoding="utf-8")


def compute_test_lock(
    workspace_dir: Path,
    runner_config_names: Sequence[str] = RUNNER_CONFIG_FILENAMES,
) -> TestLockSnapshot:
    """Compute deterministic SHA-256 hashes and snapshot contents for tests/ and runner configs (INV-13)."""
    ws = Path(workspace_dir).resolve()
    tests_dir = ws / "tests"

    test_entries: list[tuple[str, str]] = []
    if tests_dir.is_dir():
        for file_path in tests_dir.rglob("*"):
            if file_path.is_file():
                rel_posix = file_path.relative_to(ws).as_posix()
                content = file_path.read_text(encoding="utf-8")
                test_entries.append((rel_posix, content))
    test_entries.sort(key=lambda x: x[0])

    cfg_entries: list[tuple[str, str]] = []
    for name in sorted(runner_config_names):
        cfg_file = ws / name
        if cfg_file.is_file():
            rel_posix = cfg_file.relative_to(ws).as_posix()
            content = cfg_file.read_text(encoding="utf-8")
            cfg_entries.append((rel_posix, content))

    if test_entries:
        tests_payload = "".join(f"{rel}\0{cnt}\0" for rel, cnt in test_entries).encode()
        tests_hash = hashlib.sha256(tests_payload).hexdigest()
    else:
        tests_hash = hashlib.sha256(b"").hexdigest()

    if cfg_entries:
        cfg_payload = "".join(f"{rel}\0{cnt}\0" for rel, cnt in cfg_entries).encode()
        runner_config_hash = hashlib.sha256(cfg_payload).hexdigest()
    else:
        runner_config_hash = hashlib.sha256(b"").hexdigest()

    combined_hash = hashlib.sha256(f"{tests_hash}:{runner_config_hash}".encode()).hexdigest()

    saved_files: dict[str, str] = {}
    for rel, cnt in test_entries:
        saved_files[rel] = cnt
    for rel, cnt in cfg_entries:
        saved_files[rel] = cnt

    return TestLockSnapshot(
        tests_hash=tests_hash,
        runner_config_hash=runner_config_hash,
        combined_hash=combined_hash,
        saved_files=saved_files,
    )


def verify_and_restore_test_lock(
    workspace_dir: Path,
    snapshot: TestLockSnapshot,
    runner_config_names: Sequence[str] = RUNNER_CONFIG_FILENAMES,
) -> bool:
    """Verify tests/ and runner config hashes against snapshot; if altered, revert files and return False."""
    ws = Path(workspace_dir).resolve()
    current = compute_test_lock(ws, runner_config_names=runner_config_names)
    if current.combined_hash == snapshot.combined_hash:
        return True

    # Tampering detected: revert workspace to snapshot
    tests_dir = ws / "tests"
    # 1. Delete any rogue files inside tests/ that are not in snapshot
    if tests_dir.is_dir():
        for file_path in list(tests_dir.rglob("*")):
            if file_path.is_file():
                rel_posix = file_path.relative_to(ws).as_posix()
                if rel_posix not in snapshot.saved_files:
                    file_path.unlink()

    # 2. Delete any rogue runner config files that are not in snapshot
    for name in runner_config_names:
        cfg_file = ws / name
        if cfg_file.is_file():
            rel_posix = cfg_file.relative_to(ws).as_posix()
            if rel_posix not in snapshot.saved_files:
                cfg_file.unlink()

    # 3. Restore all saved files from snapshot
    for rel_posix, content in snapshot.saved_files.items():
        file_path = ws / rel_posix
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")

    return False
