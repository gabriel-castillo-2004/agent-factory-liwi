"""Targeted unit tests for StagingManager cumulative git worktree workflow."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_factory.staging_manager import StagingManager, get_staging_manager


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "factory_repo"
    repo.mkdir(parents=True, exist_ok=True)
    assert _git(repo, "init").returncode == 0
    assert _git(repo, "checkout", "-B", "main").returncode == 0
    assert _git(repo, "config", "user.name", "Test Agent").returncode == 0
    assert _git(repo, "config", "user.email", "test@agent-factory.local").returncode == 0

    (repo / "README.md").write_text("# Agent Factory Test Repo\n", encoding="utf-8")
    assert _git(repo, "add", "README.md").returncode == 0
    assert _git(repo, "commit", "-m", "chore: initial commit").returncode == 0
    return repo


def _commit_file_executor(worktree: Path, task: dict[str, Any]) -> None:
    meta = task.get("metadata") or {}
    filename = str(meta.get("filename") or "change.txt")
    content = str(meta.get("content") or task["prompt"])
    target = worktree / filename
    target.write_text(content, encoding="utf-8")
    add_res = _git(worktree, "add", filename)
    assert add_res.returncode == 0, add_res.stderr
    commit_res = _git(worktree, "commit", "-m", task["title"])
    assert commit_res.returncode == 0, commit_res.stderr


def test_ensure_worktree_creates_staging_branch_and_keeps_main_clean(
    temp_git_repo: Path,
) -> None:
    initial_main_sha = _git(temp_git_repo, "rev-parse", "main").stdout.strip()
    manager = StagingManager(repo_root=temp_git_repo)
    try:
        worktree_path = manager.ensure_worktree()
        assert worktree_path == (temp_git_repo / ".liwi-staging").resolve()
        assert worktree_path.is_dir()

        wt_branch = _git(worktree_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert wt_branch == "liwi-staging"

        main_sha_after = _git(temp_git_repo, "rev-parse", "main").stdout.strip()
        assert main_sha_after == initial_main_sha
        assert _git(temp_git_repo, "status", "--porcelain").stdout.strip() == ""

        # Idempotent second call
        assert manager.ensure_worktree() == worktree_path
    finally:
        manager.shutdown()


def test_enqueue_accumulates_two_commits_without_touching_main(
    temp_git_repo: Path,
) -> None:
    initial_main_sha = _git(temp_git_repo, "rev-parse", "main").stdout.strip()
    manager = StagingManager(
        repo_root=temp_git_repo,
        executor=_commit_file_executor,
    )
    try:
        manager.enqueue_improvement(
            title="feat: add alpha module",
            prompt="Create alpha.txt",
            metadata={"filename": "alpha.txt", "content": "alpha\n"},
        )
        manager.enqueue_improvement(
            title="feat: add beta module",
            prompt="Create beta.txt",
            metadata={"filename": "beta.txt", "content": "beta\n"},
        )

        assert manager.wait_until_idle(timeout=15.0) is True

        # main stays untouched on its initial commit
        assert _git(temp_git_repo, "rev-parse", "main").stdout.strip() == initial_main_sha
        assert not (temp_git_repo / "alpha.txt").exists()
        assert not (temp_git_repo / "beta.txt").exists()
        assert _git(temp_git_repo, "status", "--porcelain").stdout.strip() == ""

        status = manager.get_staging_status()
        assert status["pending_count"] == 2
        assert len(status["commits"]) == 2
        titles = [c["title"] for c in status["commits"]]
        assert set(titles) == {"feat: add alpha module", "feat: add beta module"}
        assert all(isinstance(c["hash"], str) and len(c["hash"]) >= 7 for c in status["commits"])
        assert status["is_building"] is False
        assert status["queue_size"] == 0
        assert status["last_error"] is None
        assert status["current_task"] is None
    finally:
        manager.shutdown()


def test_error_recovery_records_last_error_cleans_worktree_and_continues(
    temp_git_repo: Path,
) -> None:
    def flaky_executor(worktree: Path, task: dict[str, Any]) -> None:
        if task["title"] == "broken change":
            (worktree / "README.md").write_text("CORRUPTED\n", encoding="utf-8")
            (worktree / "untracked_junk.txt").write_text("junk\n", encoding="utf-8")
            raise RuntimeError("simulated staging build failure")
        _commit_file_executor(worktree, task)

    manager = StagingManager(
        repo_root=temp_git_repo,
        executor=flaky_executor,
    )
    try:
        manager.enqueue_improvement(title="broken change", prompt="Break things")
        assert manager.wait_until_idle(timeout=15.0) is True

        status_after_err = manager.get_staging_status()
        assert status_after_err["pending_count"] == 0
        assert status_after_err["is_building"] is False
        assert status_after_err["queue_size"] == 0
        assert status_after_err["last_error"] is not None
        assert "simulated staging build failure" in status_after_err["last_error"]

        wt = temp_git_repo / ".liwi-staging"
        assert _git(wt, "status", "--porcelain").stdout.strip() == ""
        assert not (wt / "untracked_junk.txt").exists()
        assert (wt / "README.md").read_text(encoding="utf-8") == "# Agent Factory Test Repo\n"

        manager.enqueue_improvement(
            title="feat: clean recovery commit",
            prompt="Create recovered.txt",
            metadata={"filename": "recovered.txt", "content": "ok\n"},
        )
        assert manager.wait_until_idle(timeout=15.0) is True

        status_after_ok = manager.get_staging_status()
        assert status_after_ok["pending_count"] == 1
        assert len(status_after_ok["commits"]) == 1
        assert status_after_ok["commits"][0]["title"] == "feat: clean recovery commit"
        assert status_after_ok["last_error"] is None
    finally:
        manager.shutdown()


def test_promote_staging_to_main_fast_forwards_and_triggers_restart_once(
    temp_git_repo: Path,
) -> None:
    restart_calls: list[str] = []

    manager = StagingManager(
        repo_root=temp_git_repo,
        executor=_commit_file_executor,
        on_restart=lambda: restart_calls.append("restarted"),
    )
    try:
        manager.enqueue_improvement(
            title="feat: first improvement",
            prompt="Add first.txt",
            metadata={"filename": "first.txt", "content": "1\n"},
        )
        manager.enqueue_improvement(
            title="feat: second improvement",
            prompt="Add second.txt",
            metadata={"filename": "second.txt", "content": "2\n"},
        )
        assert manager.wait_until_idle(timeout=15.0) is True

        result = manager.promote_staging_to_main(trigger_restart=True)
        assert result["promoted"] is True
        assert result["merged_commits"] == 2
        assert len(result["commits"]) == 2
        assert restart_calls == ["restarted"]

        main_sha = _git(temp_git_repo, "rev-parse", "main").stdout.strip()
        staging_sha = _git(temp_git_repo, "rev-parse", "liwi-staging").stdout.strip()
        wt_sha = _git(temp_git_repo / ".liwi-staging", "rev-parse", "HEAD").stdout.strip()
        assert main_sha == staging_sha == wt_sha
        assert (temp_git_repo / "first.txt").read_text(encoding="utf-8") == "1\n"
        assert (temp_git_repo / "second.txt").read_text(encoding="utf-8") == "2\n"

        status = manager.get_staging_status()
        assert status["pending_count"] == 0
        assert status["commits"] == []

        # Promoting again with no pending commits is a safe no-op
        noop = manager.promote_staging_to_main(trigger_restart=True)
        assert noop == {"promoted": False, "merged_commits": 0, "commits": []}
        assert restart_calls == ["restarted"]
    finally:
        manager.shutdown()


def test_promote_rejects_while_building_and_handles_non_ff_failure(
    temp_git_repo: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_executor(worktree: Path, task: dict[str, Any]) -> None:
        started.set()
        release.wait(timeout=10.0)
        _commit_file_executor(worktree, task)

    manager = StagingManager(
        repo_root=temp_git_repo,
        executor=blocking_executor,
    )
    try:
        manager.enqueue_improvement(
            title="feat: slow staging task",
            prompt="Create slow.txt",
            metadata={"filename": "slow.txt", "content": "slow\n"},
        )
        assert started.wait(timeout=10.0) is True

        with pytest.raises(RuntimeError, match="construcción|cola|building"):
            manager.promote_staging_to_main(trigger_restart=False)

        release.set()
        assert manager.wait_until_idle(timeout=15.0) is True

        # Create a diverging commit on main so --ff-only must fail cleanly
        (temp_git_repo / "main_only.txt").write_text("diverged\n", encoding="utf-8")
        assert _git(temp_git_repo, "add", "main_only.txt").returncode == 0
        assert _git(temp_git_repo, "commit", "-m", "chore: diverge main").returncode == 0
        main_before = _git(temp_git_repo, "rev-parse", "main").stdout.strip()

        with pytest.raises(RuntimeError, match="fast-forward|merge"):
            manager.promote_staging_to_main(trigger_restart=False)

        assert _git(temp_git_repo, "rev-parse", "main").stdout.strip() == main_before
        assert _git(temp_git_repo, "status", "--porcelain").stdout.strip() == ""
        assert manager.get_staging_status()["last_error"] is not None
    finally:
        release.set()
        manager.shutdown()


def test_get_staging_manager_singleton_per_repo(temp_git_repo: Path) -> None:
    m1 = get_staging_manager(repo_root=temp_git_repo, reset=True)
    m2 = get_staging_manager(repo_root=temp_git_repo)
    try:
        assert m1 is m2
    finally:
        m1.shutdown()
