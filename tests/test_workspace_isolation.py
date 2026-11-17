"""Tests for workspace isolation and path sanitization (A2a-1, INV-18, INV-22, INV-23)."""

import os
import subprocess
from pathlib import Path

import pytest

from agent_factory.workspace import SandboxViolationError, WorkspaceManager

INVALID_PROJECT_IDS: tuple[str, ...] = (
    "../escape",
    "..\\escape",
    "/abs/path",
    "C:\\abs\\path",
    "",
    " ",
    "-leading-dash",
    "UPPERCASE",
    "has_underscore",
    "abc\n",
    "a" * 64,
)

ESCAPE_PATHS: tuple[str, ...] = (
    "../outside.txt",
    "..\\outside.txt",
    "src/../../outside.txt",
    "tests/../../../outside.txt",
    "/etc/passwd",
    "C:\\Windows\\win.ini",
)


def _create_dir_link(link_path: Path, target_dir: Path) -> None:
    """Create a directory symlink or Windows directory junction without requiring elevation."""
    try:
        os.symlink(target_dir, link_path, target_is_directory=True)
    except OSError:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(target_dir)],
            check=True,
            capture_output=True,
            text=True,
        )


def test_workspace_manager_creates_isolated_workspace_and_resolves_valid_paths(
    tmp_path: Path,
) -> None:
    """A2a-1 (INV-18, INV-22): WorkspaceManager creates workspaces/<id>/ and writes/reads valid relative paths."""
    base_dir = tmp_path / "workspaces"
    ws_mgr = WorkspaceManager(base_dir=base_dir)

    ws_dir = ws_mgr.create_workspace(
        project_id="proj-iso-01",
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )

    expected_ws_dir = (base_dir / "proj-iso-01").resolve()
    assert ws_dir == expected_ws_dir
    assert (ws_dir / "pyproject.toml").is_file()

    written_path = ws_mgr.write_file(
        project_id="proj-iso-01",
        relative_path="src/app.ts",
        content="export const version = 1;\n",
    )
    assert written_path == expected_ws_dir / "src" / "app.ts"
    assert ws_mgr.read_file("proj-iso-01", "src/app.ts") == "export const version = 1;\n"


@pytest.mark.parametrize("bad_project_id", INVALID_PROJECT_IDS)
def test_workspace_manager_rejects_invalid_project_ids(
    tmp_path: Path,
    bad_project_id: str,
) -> None:
    """A2a-1 (INV-22): WorkspaceManager rejects any invalid project_id before touching the filesystem."""
    base_dir = tmp_path / "workspaces"
    ws_mgr = WorkspaceManager(base_dir=base_dir)

    with pytest.raises((SandboxViolationError, ValueError)):
        ws_mgr.get_workspace_dir(bad_project_id)

    with pytest.raises((SandboxViolationError, ValueError)):
        ws_mgr.create_workspace(bad_project_id)

    with pytest.raises((SandboxViolationError, ValueError)):
        ws_mgr.resolve_safe_path(bad_project_id, "src/index.ts")


@pytest.mark.parametrize("escape_path", ESCAPE_PATHS)
def test_workspace_manager_rejects_relative_traversal_and_absolute_paths(
    tmp_path: Path,
    escape_path: str,
) -> None:
    """A2a-1 (INV-18, INV-22): Relative traversal (../) and absolute paths are rejected and never touch outside files."""
    base_dir = tmp_path / "workspaces"
    sentinel = base_dir / "outside.txt"
    base_dir.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("KEEP_SAFE", encoding="utf-8")

    ws_mgr = WorkspaceManager(base_dir=base_dir)
    ws_mgr.create_workspace("proj-iso-02")

    with pytest.raises(SandboxViolationError):
        ws_mgr.resolve_safe_path("proj-iso-02", escape_path)

    with pytest.raises(SandboxViolationError):
        ws_mgr.write_file("proj-iso-02", escape_path, "OVERWRITTEN")

    with pytest.raises(SandboxViolationError):
        ws_mgr.read_file("proj-iso-02", escape_path)

    assert sentinel.read_text(encoding="utf-8") == "KEEP_SAFE"


def test_workspace_manager_rejects_symlink_or_junction_escaping_workspace(
    tmp_path: Path,
) -> None:
    """A2a-1 (INV-18, INV-22): Links inside workspaces/<id>/ that resolve outside the workspace are rejected."""
    base_dir = tmp_path / "workspaces"
    outside_dir = tmp_path / "outside_target"
    outside_dir.mkdir(parents=True, exist_ok=True)
    outside_secret = outside_dir / "secret.txt"
    outside_secret.write_text("SECRET_CONTENT", encoding="utf-8")

    ws_mgr = WorkspaceManager(base_dir=base_dir)
    ws_dir = ws_mgr.create_workspace("proj-iso-link")

    link_dir = ws_dir / "escaped_link"
    _create_dir_link(link_dir, outside_dir)

    with pytest.raises(SandboxViolationError):
        ws_mgr.resolve_safe_path("proj-iso-link", "escaped_link/secret.txt")

    with pytest.raises(SandboxViolationError):
        ws_mgr.write_file("proj-iso-link", "escaped_link/secret.txt", "TAMPERED")

    assert outside_secret.read_text(encoding="utf-8") == "SECRET_CONTENT"
