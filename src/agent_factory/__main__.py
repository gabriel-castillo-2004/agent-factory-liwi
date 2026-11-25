"""Module entrypoint allowing `python -m agent_factory` execution."""

import sys

from agent_factory.cli import main
from agent_factory.supervisor import RESTART_EXIT_CODE

__all__ = ["RESTART_EXIT_CODE", "main"]

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
