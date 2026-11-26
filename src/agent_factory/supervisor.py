"""Clean auto-restart process supervisor for Agent Factory (`python -m agent_factory.supervisor`)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

RESTART_EXIT_CODE: int = 42
DEFAULT_RESTART_DELAY_SECONDS: float = 1.5
DEFAULT_STARTUP_HEALTH_TIMEOUT_SECONDS: float = 12.0
DEFAULT_HEALTHCHECK_URL: str = "http://127.0.0.1:8000/api/autonomous/status"

logger = logging.getLogger(__name__)
_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _check_health_200(url: str, *, timeout: float = 2.0) -> bool:
    try:
        with _LOOPBACK_OPENER.open(url, timeout=timeout) as resp:
            return int(getattr(resp, "status", 0)) == 200
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def _rollback_failed_boot_commit(
    *,
    repo_root: Path,
    traceback_text: str,
    health_url: str,
    db_path: Path | str | None = None,
) -> None:
    """Record an actionable triage ticket and revert HEAD to HEAD~1 after a failed startup."""
    try:
        from agent_factory.error_triage import build_actionable_fix_ticket

        detail = (
            traceback_text.strip()
            or f"El servidor no respondió 200 OK en {health_url} dentro de {DEFAULT_STARTUP_HEALTH_TIMEOUT_SECONDS}s."
        )
        build_actionable_fix_ticket(
            "startup_watchdog",
            detail,
            context={"reverted_to": "HEAD~1", "health_url": health_url},
            db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Watchdog triage ticket skipped: %s", exc)
    subprocess.run(
        ["git", "reset", "--hard", "HEAD~1"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=20.0,
    )


def _notify_supervisor_event(
    notifier: Any | None,
    *,
    exit_code: int,
) -> None:
    """Emit best-effort classified notification for supervisor restart or unexpected exit."""
    if notifier is None:
        return
    try:
        if exit_code == RESTART_EXIT_CODE:
            notifier.notify_progress(
                "agent-factory",
                state="RESTARTING",
                summary="Reinicio automático planificado de Agent Factory.",
            )
        elif exit_code != 0:
            notifier.notify_needs_attention(
                "agent-factory",
                cause=f"El proceso de Agent Factory terminó de forma inesperada (código {exit_code}).",
                stage="SUPERVISOR",
            )
    except (OSError, RuntimeError, ValueError, TypeError):
        pass


def _detached_popen_kwargs() -> dict[str, Any]:
    """Return cross-platform Popen kwargs so child servers stay detached from parent terminal on Windows."""
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        detached = int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
        new_group = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
        kwargs["creationflags"] = detached | new_group
    else:
        kwargs["start_new_session"] = True
    return kwargs


def launch_detached_supervisor(
    args: Sequence[str] | None = None,
    *,
    executable: str | None = None,
    repo_root: Path | str | None = None,
) -> subprocess.Popen[Any]:
    """Spawn `python -m agent_factory.supervisor` completely detached from the caller terminal."""
    cmd = [executable or sys.executable, "-m", "agent_factory.supervisor", *list(args or ())]
    eff_repo = Path(repo_root).resolve() if repo_root is not None else Path.cwd()
    return subprocess.Popen(
        cmd,
        cwd=eff_repo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **_detached_popen_kwargs(),
    )


def _run_child_with_watchdog(
    cmd: Sequence[str],
    *,
    health_url: str,
    startup_timeout_seconds: float,
    health_check_fn: Callable[[str], bool],
    sleep_fn: Callable[[float], Any],
    repo_root: Path,
    db_path: Path | str | None = None,
    allow_rollback: bool = True,
) -> tuple[int, bool]:
    """Launch child server, verify health_url returns 200 within startup_timeout_seconds, else rollback HEAD~1."""
    proc = subprocess.Popen(
        list(cmd),
        cwd=repo_root,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_detached_popen_kwargs(),
    )
    stderr_lines: list[str] = []

    def _drain_stderr() -> None:
        if proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                if len(stderr_lines) < 200:
                    stderr_lines.append(line)
        except Exception as exc:  # noqa: BLE001
            logger.debug("stderr drain stopped: %s", exc)

    drain_t = threading.Thread(target=_drain_stderr, name="supervisor-stderr-drain", daemon=True)
    drain_t.start()

    deadline = time.monotonic() + max(0.2, float(startup_timeout_seconds))
    healthy = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        if health_check_fn(health_url):
            healthy = True
            break
        sleep_fn(0.25)

    if not healthy and proc.poll() is None and health_check_fn(health_url):
        healthy = True

    if not healthy:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
        drain_t.join(timeout=1.0)
        tb_text = "".join(stderr_lines)[-3500:]
        port_busy = "address already in use" in tb_text.lower() or "10048" in tb_text
        if allow_rollback and not port_busy:
            _rollback_failed_boot_commit(
                repo_root=repo_root,
                traceback_text=tb_text,
                health_url=health_url,
                db_path=db_path,
            )
            return RESTART_EXIT_CODE, True
        return int(proc.returncode or 1), False

    exit_code = int(proc.wait())
    drain_t.join(timeout=1.0)
    return exit_code, False


def run_supervisor(
    args: Sequence[str] | None = None,
    *,
    spawn_fn: Callable[[Sequence[str]], Any] | None = None,
    sleep_fn: Callable[[float], Any] = time.sleep,
    restart_delay_seconds: float = DEFAULT_RESTART_DELAY_SECONDS,
    executable: str | None = None,
    notifier: Any | None = None,
    health_url: str = DEFAULT_HEALTHCHECK_URL,
    startup_timeout_seconds: float = DEFAULT_STARTUP_HEALTH_TIMEOUT_SECONDS,
    health_check_fn: Callable[[str], bool] | None = None,
    repo_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> int:
    """Run `python -m agent_factory` in a loop with 12s startup watchdog and exit-42 restart."""
    cmd = [executable or sys.executable, "-m", "agent_factory", *list(args or ())]
    eff_repo = Path(repo_root).resolve() if repo_root is not None else Path.cwd()
    checker = health_check_fn if health_check_fn is not None else _check_health_200
    rolled_back_once = False

    while True:
        try:
            if spawn_fn is not None:
                proc = spawn_fn(cmd)
                exit_code = int(getattr(proc, "returncode", 1))
                did_rollback = False
            else:
                exit_code, did_rollback = _run_child_with_watchdog(
                    cmd,
                    health_url=health_url,
                    startup_timeout_seconds=startup_timeout_seconds,
                    health_check_fn=checker,
                    sleep_fn=sleep_fn,
                    repo_root=eff_repo,
                    db_path=db_path,
                    allow_rollback=not rolled_back_once,
                )
        except KeyboardInterrupt:
            return 130
        if did_rollback:
            rolled_back_once = True
        else:
            rolled_back_once = False
        _notify_supervisor_event(notifier, exit_code=exit_code)
        if exit_code != RESTART_EXIT_CODE:
            return exit_code
        sleep_fn(float(restart_delay_seconds))


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for supervised auto-restarting Agent Factory server."""
    eff_argv = sys.argv[1:] if argv is None else argv
    return run_supervisor(eff_argv)


if __name__ == "__main__":
    raise SystemExit(main())
