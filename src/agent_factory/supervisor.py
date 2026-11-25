"""Clean auto-restart process supervisor for Agent Factory (`python -m agent_factory.supervisor`)."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

RESTART_EXIT_CODE: int = 42
DEFAULT_RESTART_DELAY_SECONDS: float = 1.5


def run_supervisor(
    args: Sequence[str] | None = None,
    *,
    spawn_fn: Callable[[Sequence[str]], Any] | None = None,
    sleep_fn: Callable[[float], Any] = time.sleep,
    restart_delay_seconds: float = DEFAULT_RESTART_DELAY_SECONDS,
    executable: str | None = None,
) -> int:
    """Run `python -m agent_factory` in a loop, restarting only when child exits with code 42."""
    cmd = [executable or sys.executable, "-m", "agent_factory", *list(args or ())]
    runner = spawn_fn if spawn_fn is not None else subprocess.run

    while True:
        try:
            proc = runner(cmd)
        except KeyboardInterrupt:
            return 130
        exit_code = int(getattr(proc, "returncode", 1))
        if exit_code != RESTART_EXIT_CODE:
            return exit_code
        sleep_fn(float(restart_delay_seconds))


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for supervised auto-restarting Agent Factory server."""
    eff_argv = sys.argv[1:] if argv is None else argv
    return run_supervisor(eff_argv)


if __name__ == "__main__":
    raise SystemExit(main())
