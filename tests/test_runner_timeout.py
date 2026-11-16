"""Tests for SubprocessRunner timeout and child process tree termination (A2a-2, INV-02, INV-06)."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.ports.runner import CommandTimeoutError, Runner


def _is_pid_running(pid: int) -> bool:
    """Check whether a process with the given PID is still running (Windows and POSIX)."""
    if os.name == "nt":
        res = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in res.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_subprocess_runner_executes_fast_command_and_captures_output(tmp_path: Path) -> None:
    """A2a-2: SubprocessRunner implements Runner port and executes commands within timeout."""
    runner = SubprocessRunner(default_timeout_seconds=5.0)
    assert isinstance(runner, Runner)

    result = runner.run(
        command=[
            sys.executable,
            "-c",
            "import sys; print('runner-stdout-ok'); sys.stderr.write('runner-stderr-ok\\n')",
        ],
        cwd=tmp_path,
        timeout_seconds=5.0,
    )

    assert result.exit_code == 0
    assert result.timed_out is False
    assert "runner-stdout-ok" in result.stdout
    assert "runner-stderr-ok" in result.stderr


def test_subprocess_runner_enforces_timeout_and_kills_parent_and_child_processes(
    tmp_path: Path,
) -> None:
    """A2a-2 (INV-06): When a command times out, SubprocessRunner kills both the parent process and its spawned children."""
    runner = SubprocessRunner(default_timeout_seconds=1.0)
    child_pid_file = tmp_path / "spawned_child.pid"

    parent_script = (
        "import subprocess, sys, time, pathlib; "
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(p.pid), encoding='utf-8'); "
        "time.sleep(60)"
    )

    start_time = time.monotonic()
    with pytest.raises(CommandTimeoutError):
        runner.run(
            command=[sys.executable, "-c", parent_script],
            cwd=tmp_path,
            timeout_seconds=0.6,
        )
    elapsed = time.monotonic() - start_time

    # Must not block waiting for the 60-second sleep
    assert elapsed < 10.0
    assert child_pid_file.is_file()

    child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
    # Allow brief OS process table cleanup
    time.sleep(0.1)
    assert _is_pid_running(child_pid) is False
