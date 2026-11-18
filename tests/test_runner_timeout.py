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


def test_subprocess_runner_resolves_cmd_executable_via_effective_env_path(tmp_path: Path) -> None:
    """SubprocessRunner resolves bare command names (including .cmd shims on Windows) using effective env PATH with shell=False."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    runner = SubprocessRunner(default_timeout_seconds=5.0)

    if os.name == "nt":
        shim = bin_dir / "agydummy.cmd"
        shim.write_text("@echo off\r\necho shim-ok %1\r\n", encoding="utf-8")
        env = {
            "PATH": str(bin_dir),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
            "COMSPEC": os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
            "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
        }
    else:
        shim = bin_dir / "agydummy"
        shim.write_text("#!/bin/sh\necho shim-ok \"$1\"\n", encoding="utf-8")
        shim.chmod(0o755)
        env = {"PATH": str(bin_dir)}

    res = runner.run(command=["agydummy", "hello"], cwd=tmp_path, env=env)
    assert res.exit_code == 0
    assert "shim-ok hello" in res.stdout


def test_subprocess_runner_decodes_utf8_and_replaces_invalid_bytes(tmp_path: Path) -> None:
    """SubprocessRunner decodes stdout and stderr as UTF-8 (preventing Windows CP1252 mojibake) and replaces malformed bytes safely."""
    runner = SubprocessRunner(default_timeout_seconds=5.0)
    script = (
        "import sys; "
        "sys.stdout.buffer.write('¿Ayúdame con el menú?\\n'.encode('utf-8')); "
        "sys.stderr.buffer.write(b'Aviso UTF-8: \\xc3\\xa1 y byte malo \\xff\\n')"
    )
    res = runner.run(command=[sys.executable, "-c", script], cwd=tmp_path)
    assert res.exit_code == 0
    assert "¿Ayúdame con el menú?" in res.stdout
    assert "Ãº" not in res.stdout
    assert "Aviso UTF-8: á" in res.stderr
    assert "\ufffd" in res.stderr
