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


def test_promote_rebases_diverged_main_before_ff_merge_and_syncs_empty_worktree(
    temp_git_repo: Path,
) -> None:
    """When main advances before or after a staging build, empty staging syncs to main HEAD before building, promote() rebases diverged non-conflicting commits and fast-forwards cleanly, and real conflicts abort rebase cleanly."""
    restart_calls: list[str] = []
    manager = StagingManager(
        repo_root=temp_git_repo,
        executor=_commit_file_executor,
        on_restart=lambda: restart_calls.append("restarted"),
    )
    try:
        # 1. Create worktree at initial commit, then advance main while staging has 0 pending commits
        wt = manager.ensure_worktree()
        (temp_git_repo / "base_ahead.txt").write_text("from-main-first\n", encoding="utf-8")
        assert _git(temp_git_repo, "add", "base_ahead.txt").returncode == 0
        assert _git(temp_git_repo, "commit", "-m", "chore: advance main before build").returncode == 0
        main_ahead_sha = _git(temp_git_repo, "rev-parse", "main").stdout.strip()

        # Enqueuing a new build when staging queue/commits are empty resets .liwi-staging to main HEAD first
        seen_base_in_build: list[bool] = []

        def _check_base_and_commit(worktree: Path, task: dict[str, Any]) -> None:
            seen_base_in_build.append((worktree / "base_ahead.txt").is_file())
            _commit_file_executor(worktree, task)

        manager._executor = _check_base_and_commit
        manager.enqueue_improvement(
            title="feat: feature built on latest main",
            prompt="Add feat_latest.txt",
            metadata={"filename": "feat_latest.txt", "content": "ok\n"},
        )
        assert manager.wait_until_idle(timeout=15.0) is True
        assert seen_base_in_build == [True]
        assert main_ahead_sha in _git(wt, "log", "--format=%H", "-n", "5").stdout

        # 2. Diverge main AFTER staging already has a ready commit (disjoint file -> clean rebase)
        (temp_git_repo / "main_diverged_after.txt").write_text("main-update\n", encoding="utf-8")
        assert _git(temp_git_repo, "add", "main_diverged_after.txt").returncode == 0
        assert _git(temp_git_repo, "commit", "-m", "chore: diverge main after staging build").returncode == 0

        res = manager.promote(trigger_restart=True)
        assert res["promoted"] is True
        assert res["merged_commits"] == 1
        assert restart_calls == ["restarted"]
        assert (temp_git_repo / "feat_latest.txt").read_text(encoding="utf-8") == "ok\n"
        assert (temp_git_repo / "main_diverged_after.txt").read_text(encoding="utf-8") == "main-update\n"
        assert manager.get_staging_status()["pending_count"] == 0

        main_sha = _git(temp_git_repo, "rev-parse", "main").stdout.strip()
        staging_sha = _git(temp_git_repo, "rev-parse", "liwi-staging").stdout.strip()
        wt_sha = _git(wt, "rev-parse", "HEAD").stdout.strip()
        assert main_sha == staging_sha == wt_sha

        # 3. Real conflict in promote() aborts rebase cleanly without leaving worktree stuck mid-rebase
        manager.enqueue_improvement(
            title="feat: edit shared README",
            prompt="Update README on staging",
            metadata={"filename": "README.md", "content": "STAGING CONFLICT CONTENT\n"},
        )
        assert manager.wait_until_idle(timeout=15.0) is True

        (temp_git_repo / "README.md").write_text("MAIN CONFLICT CONTENT\n", encoding="utf-8")
        assert _git(temp_git_repo, "add", "README.md").returncode == 0
        assert _git(temp_git_repo, "commit", "-m", "chore: conflicting README edit on main").returncode == 0

        with pytest.raises(RuntimeError, match="rebase|conflicto|fast-forward"):
            manager.promote(trigger_restart=False)

        assert not (wt / ".git").is_dir()  # worktree .git is file; check rebase state via git status
        assert _git(wt, "status", "--porcelain").stdout.strip() == ""
        assert (wt / "README.md").read_text(encoding="utf-8") == "STAGING CONFLICT CONTENT\n"
        assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "liwi-staging"
    finally:
        manager.shutdown()
