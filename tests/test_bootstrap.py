"""Bootstrap tests for Phase 0 (INV-03, limits and runner setup)."""

import socket
import tomllib
from pathlib import Path

import pytest
import pytest_socket

import agent_factory


def test_trivial_pass() -> None:
    """Trivial test that passes to verify test discovery and runner setup."""
    assert agent_factory is not None


def test_network_socket_blocked() -> None:
    """Verify that network socket access is blocked (INV-03)."""
    with pytest.raises(pytest_socket.SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_pytest_timeout_configured_in_pyproject() -> None:
    """Verify that timeout and timeout_method are configured in pyproject.toml."""
    pyproject_path = Path("pyproject.toml")
    assert pyproject_path.is_file(), "pyproject.toml must exist at repo root"

    with pyproject_path.open("rb") as f:
        config = tomllib.load(f)

    pytest_ini = config.get("tool", {}).get("pytest", {}).get("ini_options", {})
    assert "timeout" in pytest_ini, "'timeout' must be in [tool.pytest.ini_options]"
    assert pytest_ini["timeout"] == 30
    assert "timeout_method" in pytest_ini, "'timeout_method' must be in [tool.pytest.ini_options]"
    assert pytest_ini["timeout_method"] == "thread"
