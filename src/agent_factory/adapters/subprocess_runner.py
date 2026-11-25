"""Multiplatform SubprocessRunner adapter with timeout and process-tree termination (A2a-2, INV-02, INV-06)."""

import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_factory.ports.runner import CommandResult, CommandTimeoutError, Runner

_PYTHON_VENV_TOOLS: frozenset[str] = frozenset({"pytest", "ruff", "uvicorn", "pip"})


def _safe_which(exe: str, search_path: str | None = None) -> str | None:
    """Call shutil.which while remaining compatible with 1-arg test monkeypatches."""
    if search_path is None:
        return shutil.which(exe)
    try:
        return shutil.which(exe, path=search_path)
    except TypeError:
        return shutil.which(exe)


def _extra_bin_dirs(exe_stem: str) -> list[str]:
    """Return active Python Scripts/bin directory for unresolved Python CLI tools."""
    if exe_stem in _PYTHON_VENV_TOOLS:
        py_dir = str(Path(sys.executable).resolve().parent)
        if py_dir:
            return [py_dir]
    return []


def _inspect_env_path(env: Mapping[str, str] | None) -> tuple[bool, str | None]:
    """Return (restricted_path, effective_search_path) inspecting env PATH case-insensitively."""
    if env is None:
        return False, None
    found_path: str | None = None
    has_key = False
    for k, v in env.items():
        if k.upper() == "PATH":
            has_key = True
            found_path = v
            break
    if not has_key:
        return False, None
    host_path = os.environ.get("PATH", "")
    assert found_path is not None
    restricted = found_path != host_path and not (bool(host_path) and found_path.endswith(host_path))
    return restricted, found_path


def resolve_executable(
    binary: str,
    env: Mapping[str, str] | None = None,
    *,
    include_host_fallbacks: bool = True,
) -> str | None:
    """Resolve an executable via shutil.which (supporting Windows shims and local venv/agy dirs)."""
    clean = str(binary or "").strip()
    if not clean:
        return None
    if os.sep in clean or (os.altsep is not None and os.altsep in clean):
        resolved_path = _safe_which(clean)
        if resolved_path:
            return resolved_path
        return clean if Path(clean).is_file() else None

    restricted_path, search_path = _inspect_env_path(env)
    exe_stem = Path(clean).stem.lower()
    if (
        include_host_fallbacks
        and not restricted_path
        and env is None
        and exe_stem in _PYTHON_VENV_TOOLS
    ):
        py_resolved = _safe_which(clean, str(Path(sys.executable).resolve().parent))
        if py_resolved:
            return py_resolved

    resolved = _safe_which(clean, search_path)
    if resolved:
        return resolved

    if include_host_fallbacks and not restricted_path:
        extra_dirs = _extra_bin_dirs(exe_stem)
        if extra_dirs:
            return _safe_which(clean, os.pathsep.join(extra_dirs))
    return None


def resolve_command(
    command: Sequence[str],
    env: Mapping[str, str] | None = None,
    *,
    include_host_fallbacks: bool = True,
) -> list[str]:
    """Resolve bare argv[0] executables (such as .cmd shims on Windows) via effective PATH."""
    cmd_list = [str(part) for part in command]
    if not cmd_list:
        return cmd_list
    resolved = resolve_executable(
        cmd_list[0],
        env=env,
        include_host_fallbacks=include_host_fallbacks,
    )
    if resolved:
        cmd_list[0] = resolved
    return cmd_list


_resolve_command = resolve_command


def _close_pipes(proc: subprocess.Popen[str]) -> None:
    """Close subprocess stdio pipes cleanly after completion or timeout."""
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


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
        input: str | bytes | None = None,
    ) -> CommandResult:
        """Execute command in cwd with timeout, killing parent and all child processes on timeout."""
        if isinstance(command, (str, bytes)) or not command:
            raise ValueError("command must be a non-empty sequence of strings")

        timeout = (
            timeout_seconds if timeout_seconds is not None else self.default_timeout_seconds
        )
        cwd_path = Path(cwd).resolve()
        cmd_list = _resolve_command(command, env)

        input_str: str | None = None
        if input is not None:
            input_str = (
                input.decode("utf-8", errors="replace")
                if isinstance(input, bytes)
                else str(input)
            )

        if os.name == "nt":
            # On Windows, start process; on timeout kill entire process tree with taskkill
            proc = subprocess.Popen(
                cmd_list,
                cwd=str(cwd_path),
                stdin=subprocess.PIPE if input_str is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=dict(env) if env is not None else None,
            )
            try:
                stdout, stderr = proc.communicate(input=input_str, timeout=timeout)
                return CommandResult(
                    exit_code=proc.returncode,
                    stdout=stdout or "",
                    stderr=stderr or "",
                    timed_out=False,
                )
            except subprocess.TimeoutExpired as exc:
                taskkill_cmd = resolve_command(["taskkill", "/F", "/T", "/PID", str(proc.pid)])
                try:
                    subprocess.run(
                        taskkill_cmd,
                        capture_output=True,
                        check=False,
                    )
                except OSError:
                    pass
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                _close_pipes(proc)
                raise CommandTimeoutError(
                    f"Command '{cmd_list}' timed out after {timeout} seconds"
                ) from exc
        else:
            # On POSIX, use a new process group session
            proc = subprocess.Popen(
                cmd_list,
                cwd=str(cwd_path),
                stdin=subprocess.PIPE if input_str is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
                env=dict(env) if env is not None else None,
            )
            try:
                stdout, stderr = proc.communicate(input=input_str, timeout=timeout)
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
                _close_pipes(proc)
                raise CommandTimeoutError(
                    f"Command '{cmd_list}' timed out after {timeout} seconds"
                ) from exc
