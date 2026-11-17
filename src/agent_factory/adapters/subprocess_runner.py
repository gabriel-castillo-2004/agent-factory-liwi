"""Multiplatform SubprocessRunner adapter with timeout and process-tree termination (A2a-2, INV-02, INV-06)."""

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_factory.ports.runner import CommandResult, CommandTimeoutError, Runner


class SubprocessRunner(Runner):
    """Multiplatform command runner with timeout and full child process-tree termination."""

    def __init__(self, default_timeout_seconds: float = 30.0) -> None:
        self.default_timeout_seconds = default_timeout_seconds

    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        timeout_seconds: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Execute command in cwd with timeout, killing parent and all child processes on timeout."""
        if isinstance(command, (str, bytes)) or not command:
            raise ValueError("command must be a non-empty sequence of strings")

        timeout = (
            timeout_seconds if timeout_seconds is not None else self.default_timeout_seconds
        )
        cwd_path = Path(cwd).resolve()

        if os.name == "nt":
            # On Windows, start process; on timeout kill entire process tree with taskkill
            proc = subprocess.Popen(
                list(command),
                cwd=str(cwd_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=dict(env) if env is not None else None,
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
                return CommandResult(
                    exit_code=proc.returncode,
                    stdout=stdout or "",
                    stderr=stderr or "",
                    timed_out=False,
                )
            except subprocess.TimeoutExpired as exc:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                )
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise CommandTimeoutError(
                    f"Command '{list(command)}' timed out after {timeout} seconds"
                ) from exc
        else:
            # On POSIX, use a new process group session
            proc = subprocess.Popen(
                list(command),
                cwd=str(cwd_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                env=dict(env) if env is not None else None,
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
                return CommandResult(
                    exit_code=proc.returncode,
                    stdout=stdout or "",
                    stderr=stderr or "",
                    timed_out=False,
                )
            except subprocess.TimeoutExpired as exc:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except OSError:
                    pass
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise CommandTimeoutError(
                    f"Command '{list(command)}' timed out after {timeout} seconds"
                ) from exc
