"""Shared pytest fixtures for offline test isolation (INV-03)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


def _agy_candidate_filenames() -> frozenset[str]:
    names = {"agy", "agy.exe", "agy.cmd", "agy.bat", "agy.com"}
    for ext in os.environ.get("PATHEXT", "").split(os.pathsep if os.name != "nt" else ";"):
        clean_ext = ext.strip().lower()
        if clean_ext:
            names.add(f"agy{clean_ext}")
    return frozenset(names)


def _dir_contains_host_agy(dir_path: Path, agy_names: frozenset[str]) -> bool:
    try:
        with os.scandir(dir_path) as it:
            for entry in it:
                if entry.name.lower() in agy_names and entry.is_file():
                    return True
    except OSError:
        return False
    return False


@pytest.fixture(autouse=True)
def _isolate_host_agy_from_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Filter pre-existing system PATH directories containing real agy binaries so tests remain isolated (INV-03)."""
    raw_path = os.environ.get("PATH", "")
    if not raw_path:
        return

    agy_names = _agy_candidate_filenames()
    tmp_root = Path(tempfile.gettempdir()).resolve()
    kept: list[str] = []
    for raw_entry in raw_path.split(os.pathsep):
        cleaned = raw_entry.strip().strip('"')
        if cleaned:
            candidate = Path(cleaned)
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            if (
                not resolved.is_relative_to(tmp_root)
                and candidate.is_dir()
                and _dir_contains_host_agy(candidate, agy_names)
            ):
                continue
        kept.append(raw_entry)

    monkeypatch.setenv("PATH", os.pathsep.join(kept))
