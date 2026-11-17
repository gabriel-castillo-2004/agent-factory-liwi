"""Module entrypoint allowing `python -m agent_factory` execution."""

import sys

from agent_factory.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
