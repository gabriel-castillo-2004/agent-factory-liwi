"""Isolated workspace sandbox and deterministic test-lock hash verification (INV-13, INV-18, INV-22)."""

import base64
import hashlib
import os
import shutil
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import BaseModel, Field

from agent_factory.domain.models import validate_project_id

_CACHE_DIR_NAMES: frozenset[str] = frozenset({"__pycache__", ".pytest_cache"})
_BYTECODE_SUFFIXES: frozenset[str] = frozenset({".pyc", ".pyo"})


def is_python_cache_path(path: str | Path) -> bool:
    """Return True if path refers to Python cache directories or bytecode (*.pyc/*.pyo)."""
    norm = str(path).strip().replace("\\", "/")
    if not norm:
        return False
    parts = [part for part in PurePosixPath(norm).parts if part and part != "."]
    if any(part in _CACHE_DIR_NAMES for part in parts):
        return True
    lower = norm.lower()
    return any(lower.endswith(ext) for ext in _BYTECODE_SUFFIXES)

RUNNER_CONFIG_FILENAMES: tuple[str, ...] = (
    "pyproject.toml",
    "pytest.ini",
    "package.json",
    "vitest.config.ts",
    "vitest.config.js",
    "playwright.config.ts",
    "playwright.config.js",
    "playwright.config.mjs",
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
    saved_binary_files: dict[str, str] = Field(default_factory=dict)


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

    def remove_workspace(self, project_id: str) -> bool:
        """Safely remove workspaces/<project_id>/ inside base_dir, clearing readonly .git files (INV-22)."""
        ws_dir = self.get_workspace_dir(project_id)
        raw_path = self.base_dir / project_id
        if raw_path.is_symlink():
            raise SandboxViolationError(
                f"Refusing to remove symlinked workspace '{raw_path}' (INV-22)"
            )
        if not ws_dir.exists():
            return False

        def _handle_readonly(func: object, path: str, _exc: object) -> None:
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
            if callable(func):
                func(path)

        if sys.version_info >= (3, 12):
            shutil.rmtree(ws_dir, onexc=_handle_readonly)
        else:
            shutil.rmtree(ws_dir, onerror=_handle_readonly)
        return True

    def resolve_safe_path(self, project_id: str, relative_path: str | Path) -> Path:
        """Resolve relative_path inside workspaces/<project_id>/ and reject any traversal or symlink escape."""
        ws_dir = self.get_workspace_dir(project_id)
        path_str = str(relative_path).strip()
        if not path_str:
            raise SandboxViolationError("Empty relative path is invalid")

        if "\\" in str(relative_path):
            raise SandboxViolationError(
                f"Backslash in path rejected: {relative_path}"
            )

        win_path = PureWindowsPath(path_str)
        if (
            any(part == ".." for part in win_path.parts)
            or win_path.is_absolute()
            or path_str.startswith(("/", "\\"))
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

    resolve_path = resolve_safe_path

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


def _read_lock_entry(file_path: Path) -> tuple[bytes, str | None]:
    """Read file as raw bytes; normalize LF for UTF-8 text files and preserve raw bytes for binary."""
    raw_bytes = file_path.read_bytes()
    if b"\x00" not in raw_bytes:
        try:
            text = raw_bytes.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            return text.encode("utf-8"), text
        except UnicodeDecodeError:
            pass
    return raw_bytes, None


def _hash_lock_entries(entries: Sequence[tuple[str, bytes]]) -> str:
    """Compute deterministic SHA-256 digest over ordered (rel_posix, raw_bytes) pairs."""
    if not entries:
        return hashlib.sha256(b"").hexdigest()
    hasher = hashlib.sha256()
    for rel_posix, data in entries:
        hasher.update(rel_posix.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(data)
        hasher.update(b"\0")
    return hasher.hexdigest()


def compute_test_lock(
    workspace_dir: Path,
    runner_config_names: Sequence[str] = RUNNER_CONFIG_FILENAMES,
) -> TestLockSnapshot:
    """Compute deterministic SHA-256 hashes and byte-safe snapshots for tests/ and runner configs (INV-13)."""
    ws = Path(workspace_dir).resolve()
    tests_dir = ws / "tests"

    test_entries: list[tuple[str, bytes, str | None]] = []
    if tests_dir.is_dir():
        for file_path in tests_dir.rglob("*"):
            if file_path.is_file():
                rel_posix = file_path.relative_to(ws).as_posix()
                if is_python_cache_path(rel_posix):
                    continue
                hash_bytes, text_val = _read_lock_entry(file_path)
                test_entries.append((rel_posix, hash_bytes, text_val))
    test_entries.sort(key=lambda x: x[0])

    cfg_entries: list[tuple[str, bytes, str | None]] = []
    for name in sorted(runner_config_names):
        cfg_file = ws / name
        if cfg_file.is_file():
            rel_posix = cfg_file.relative_to(ws).as_posix()
            hash_bytes, text_val = _read_lock_entry(cfg_file)
            cfg_entries.append((rel_posix, hash_bytes, text_val))

    tests_hash = _hash_lock_entries([(rel, data) for rel, data, _ in test_entries])
    runner_config_hash = _hash_lock_entries([(rel, data) for rel, data, _ in cfg_entries])
    combined_hash = hashlib.sha256(f"{tests_hash}:{runner_config_hash}".encode()).hexdigest()

    saved_files: dict[str, str] = {}
    saved_binary_files: dict[str, str] = {}
    for rel, data, text_val in test_entries + cfg_entries:
        if text_val is not None:
            saved_files[rel] = text_val
        else:
            saved_binary_files[rel] = base64.b64encode(data).decode("ascii")

    return TestLockSnapshot(
        tests_hash=tests_hash,
        runner_config_hash=runner_config_hash,
        combined_hash=combined_hash,
        saved_files=saved_files,
        saved_binary_files=saved_binary_files,
    )


def verify_and_restore_test_lock(
    workspace_dir: Path,
    snapshot: TestLockSnapshot,
    runner_config_names: Sequence[str] = RUNNER_CONFIG_FILENAMES,
) -> bool:
    """Verify tests/ and runner config hashes against snapshot; if altered, revert files via bytes and return False."""
    ws = Path(workspace_dir).resolve()
    current = compute_test_lock(ws, runner_config_names=runner_config_names)
    if current.combined_hash == snapshot.combined_hash:
        return True

    known_paths = set(snapshot.saved_files) | set(snapshot.saved_binary_files)

    # Tampering detected: revert workspace to snapshot
    tests_dir = ws / "tests"
    # 1. Delete any rogue files inside tests/ that are not in snapshot (preserve cache artifacts)
    if tests_dir.is_dir():
        for file_path in list(tests_dir.rglob("*")):
            if file_path.is_file():
                rel_posix = file_path.relative_to(ws).as_posix()
                if is_python_cache_path(rel_posix):
                    continue
                if rel_posix not in known_paths:
                    file_path.unlink()

    # 2. Delete any rogue runner config files that are not in snapshot
    for name in runner_config_names:
        cfg_file = ws / name
        if cfg_file.is_file():
            rel_posix = cfg_file.relative_to(ws).as_posix()
            if rel_posix not in known_paths:
                cfg_file.unlink()

    # 3. Restore all saved text and binary files from snapshot using raw bytes
    for rel_posix, content in snapshot.saved_files.items():
        file_path = ws / rel_posix
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content.encode("utf-8"))

    for rel_posix, b64_data in snapshot.saved_binary_files.items():
        file_path = ws / rel_posix
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(base64.b64decode(b64_data))

    return False
