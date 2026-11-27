"""Unit tests for SubprocessRunner timeout handling and _terminate_process_tree helper."""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_factory.adapters import subprocess_runner
from agent_factory.adapters.subprocess_runner import (
    SubprocessRunner,
    _terminate_process_tree,
)
from agent_factory.ports.runner import CommandResult, CommandTimeoutError


class _FakeOs:
    """Proxy around os for cross-platform os.name tests without breaking pathlib.Path."""

    def __init__(self, *, name: str, **overrides: object) -> None:
        self.name = name
        self._overrides = overrides

    def __getattr__(self, item: str) -> object:
        if item in self._overrides:
            return self._overrides[item]
        return getattr(os, item)


def _make_mock_proc(
    *,
    pid: int = 1234,
    returncode: int = 0,
    communicate_return: tuple[str, str] | None = ("", ""),
    communicate_side_effect: Exception | None = None,
    wait_side_effect: object | None = None,
    kill_side_effect: Exception | None = None,
) -> MagicMock:
    """Create a mock Popen instance with controllable stdio pipes and lifecycle hooks."""
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = returncode
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()
    if communicate_side_effect is not None:
        proc.communicate.side_effect = communicate_side_effect
    else:
        proc.communicate.return_value = communicate_return
    if wait_side_effect is not None:
        proc.wait.side_effect = wait_side_effect
    else:
        proc.wait.return_value = returncode
    if kill_side_effect is not None:
        proc.kill.side_effect = kill_side_effect
    return proc


def test_run_normal_execution_without_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal execution returns CommandResult and never triggers timeout cleanup."""
    mock_proc = _make_mock_proc(
        pid=2001,
        returncode=0,
        communicate_return=("ok-stdout\n", "ok-stderr\n"),
    )
    popen_mock = MagicMock(return_value=mock_proc)
    run_mock = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", popen_mock)
    monkeypatch.setattr(subprocess, "run", run_mock)

    runner = SubprocessRunner(default_timeout_seconds=10.0)
    result = runner.run(command=["echo", "hello"], cwd=tmp_path, timeout_seconds=4.0)

    assert result == CommandResult(
        exit_code=0,
        stdout="ok-stdout\n",
        stderr="ok-stderr\n",
        timed_out=False,
    )
    mock_proc.communicate.assert_called_once_with(input=None, timeout=4.0)
    run_mock.assert_not_called()
    mock_proc.kill.assert_not_called()


def test_timeout_windows_terminates_process_tree_with_taskkill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows timeout, runner invokes taskkill /F /T /PID <pid> and raises CommandTimeoutError."""
    monkeypatch.setattr(subprocess_runner, "os", _FakeOs(name="nt"))
    mock_proc = _make_mock_proc(
        pid=4321,
        communicate_side_effect=subprocess.TimeoutExpired(cmd=["agy"], timeout=2.0),
    )
    popen_mock = MagicMock(return_value=mock_proc)
    run_mock = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=["taskkill", "/F", "/T", "/PID", "4321"],
            returncode=0,
            stdout="SUCCESS",
            stderr="",
        )
    )
    monkeypatch.setattr(subprocess, "Popen", popen_mock)
    monkeypatch.setattr(subprocess, "run", run_mock)

    runner = SubprocessRunner(default_timeout_seconds=2.0)
    with pytest.raises(CommandTimeoutError, match="timed out after 2.0 seconds"):
        runner.run(command=["agy", "run"], cwd=tmp_path, timeout_seconds=2.0)

    run_mock.assert_called_once()
    call_args, call_kwargs = run_mock.call_args
    taskkill_cmd = call_args[0]
    assert Path(taskkill_cmd[0]).stem.lower() == "taskkill"
    assert taskkill_cmd[1:] == ["/F", "/T", "/PID", "4321"]
    assert call_kwargs["stdin"] == subprocess.DEVNULL
    assert call_kwargs["capture_output"] is True
    assert call_kwargs["check"] is False
    assert call_kwargs["timeout"] > 0

    mock_proc.wait.assert_called_once_with(timeout=2.0)
    mock_proc.kill.assert_not_called()
    mock_proc.stdin.close.assert_called_once()
    mock_proc.stdout.close.assert_called_once()
    mock_proc.stderr.close.assert_called_once()


def test_timeout_posix_terminates_process_group_with_killpg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On POSIX timeout, runner starts a new session and kills the process group via os.killpg."""
    getpgid_mock = MagicMock(return_value=8765)
    killpg_mock = MagicMock()
    monkeypatch.setattr(
        subprocess_runner,
        "os",
        _FakeOs(name="posix", getpgid=getpgid_mock, killpg=killpg_mock),
    )
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)

    mock_proc = _make_mock_proc(
        pid=8765,
        communicate_side_effect=subprocess.TimeoutExpired(cmd=["agy"], timeout=3.0),
    )
    popen_mock = MagicMock(return_value=mock_proc)
    run_mock = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", popen_mock)
    monkeypatch.setattr(subprocess, "run", run_mock)

    runner = SubprocessRunner(default_timeout_seconds=3.0)
    with pytest.raises(CommandTimeoutError, match="timed out after 3.0 seconds"):
        runner.run(command=["agy", "run"], cwd=tmp_path, timeout_seconds=3.0)

    assert popen_mock.call_args.kwargs["start_new_session"] is True
    getpgid_mock.assert_called_once_with(8765)
    killpg_mock.assert_called_once_with(8765, 9)
    run_mock.assert_not_called()
    mock_proc.wait.assert_called_once_with(timeout=2.0)
    mock_proc.kill.assert_not_called()
    mock_proc.stdin.close.assert_called_once()
    mock_proc.stdout.close.assert_called_once()
    mock_proc.stderr.close.assert_called_once()


def test_timeout_windows_falls_back_to_proc_kill_when_taskkill_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When taskkill raises OSError, runner falls back to proc.kill() and raises CommandTimeoutError."""
    monkeypatch.setattr(subprocess_runner, "os", _FakeOs(name="nt"))
    mock_proc = _make_mock_proc(
        pid=5555,
        communicate_side_effect=subprocess.TimeoutExpired(cmd=["agy"], timeout=1.5),
    )
    popen_mock = MagicMock(return_value=mock_proc)
    run_mock = MagicMock(side_effect=OSError("taskkill not found"))
    monkeypatch.setattr(subprocess, "Popen", popen_mock)
    monkeypatch.setattr(subprocess, "run", run_mock)

    runner = SubprocessRunner(default_timeout_seconds=1.5)
    with pytest.raises(CommandTimeoutError, match="timed out after 1.5 seconds"):
        runner.run(command=["agy", "run"], cwd=tmp_path, timeout_seconds=1.5)

    run_mock.assert_called_once()
    mock_proc.kill.assert_called_once()
    mock_proc.wait.assert_called_once_with(timeout=2.0)
    mock_proc.stdout.close.assert_called_once()
    mock_proc.stderr.close.assert_called_once()


def test_timeout_windows_falls_back_to_proc_kill_when_taskkill_returns_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When taskkill exits non-zero, runner falls back to proc.kill() without masking timeout."""
    monkeypatch.setattr(subprocess_runner, "os", _FakeOs(name="nt"))
    mock_proc = _make_mock_proc(
        pid=6666,
        communicate_side_effect=subprocess.TimeoutExpired(cmd=["agy"], timeout=1.0),
    )
    popen_mock = MagicMock(return_value=mock_proc)
    run_mock = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=["taskkill", "/F", "/T", "/PID", "6666"],
            returncode=128,
            stdout="",
            stderr="ERROR: The process was not found.",
        )
    )
    monkeypatch.setattr(subprocess, "Popen", popen_mock)
    monkeypatch.setattr(subprocess, "run", run_mock)

    runner = SubprocessRunner(default_timeout_seconds=1.0)
    with pytest.raises(CommandTimeoutError, match="timed out after 1.0 seconds"):
        runner.run(command=["agy", "run"], cwd=tmp_path, timeout_seconds=1.0)

    run_mock.assert_called_once()
    mock_proc.kill.assert_called_once()


def test_timeout_windows_falls_back_to_proc_kill_when_wait_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When taskkill returns 0 but proc.wait times out, runner falls back to proc.kill()."""
    monkeypatch.setattr(subprocess_runner, "os", _FakeOs(name="nt"))
    mock_proc = _make_mock_proc(
        pid=6789,
        communicate_side_effect=subprocess.TimeoutExpired(cmd=["agy"], timeout=1.0),
        wait_side_effect=[subprocess.TimeoutExpired(cmd=["agy"], timeout=2.0), -9],
    )
    run_mock = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=["taskkill", "/F", "/T", "/PID", "6789"],
            returncode=0,
            stdout="SUCCESS",
            stderr="",
        )
    )
    monkeypatch.setattr(subprocess, "Popen", MagicMock(return_value=mock_proc))
    monkeypatch.setattr(subprocess, "run", run_mock)

    runner = SubprocessRunner(default_timeout_seconds=1.0)
    with pytest.raises(CommandTimeoutError, match="timed out after 1.0 seconds"):
        runner.run(command=["agy", "run"], cwd=tmp_path, timeout_seconds=1.0)

    run_mock.assert_called_once()
    mock_proc.kill.assert_called_once()
    assert mock_proc.wait.call_count == 2


def test_timeout_suppresses_secondary_cleanup_errors_from_kill_and_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Secondary OSError exceptions during fallback kill/wait never mask CommandTimeoutError."""
    monkeypatch.setattr(
        subprocess_runner,
        "os",
        _FakeOs(
            name="posix",
            getpgid=MagicMock(side_effect=ProcessLookupError("gone")),
            killpg=MagicMock(),
        ),
    )
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)

    mock_proc = _make_mock_proc(
        pid=7777,
        communicate_side_effect=subprocess.TimeoutExpired(cmd=["agy"], timeout=1.0),
        kill_side_effect=OSError("already terminated"),
        wait_side_effect=OSError("no child process"),
    )
    monkeypatch.setattr(subprocess, "Popen", MagicMock(return_value=mock_proc))

    runner = SubprocessRunner(default_timeout_seconds=1.0)
    with pytest.raises(CommandTimeoutError, match="timed out after 1.0 seconds"):
        runner.run(command=["agy", "run"], cwd=tmp_path, timeout_seconds=1.0)

    mock_proc.kill.assert_called_once()
    mock_proc.stdout.close.assert_called_once()


def test_terminate_process_tree_helper_handles_invalid_pid_and_timeout_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_terminate_process_tree rejects non-positive PIDs and absorbs taskkill TimeoutExpired."""
    run_mock = MagicMock()
    killpg_mock = MagicMock()
    monkeypatch.setattr(subprocess, "run", run_mock)
    monkeypatch.setattr(subprocess_runner, "os", _FakeOs(name="posix", killpg=killpg_mock))

    assert _terminate_process_tree(0) is False
    assert _terminate_process_tree(-42) is False
    run_mock.assert_not_called()
    killpg_mock.assert_not_called()

    monkeypatch.setattr(subprocess_runner, "os", _FakeOs(name="nt"))
    timeout_run_mock = MagicMock(
        side_effect=subprocess.TimeoutExpired(cmd=["taskkill"], timeout=5.0)
    )
    monkeypatch.setattr(subprocess, "run", timeout_run_mock)

    assert _terminate_process_tree(9999) is False
    timeout_run_mock.assert_called_once()
