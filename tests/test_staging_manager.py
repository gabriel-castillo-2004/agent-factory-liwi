"""Targeted unit tests for StagingManager cumulative git worktree workflow."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_factory.parallel_improver import ParallelPlan, ParallelSubtask
from agent_factory.staging_manager import (
    DEFAULT_FORBIDDEN_DIFF_PREFIXES,
    StagingManager,
    get_staging_manager,
    validate_diff_scope,
)


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


def test_staging_manager_429_preserves_dirty_worktree_wip_and_resumes_with_diff(
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    import time
    from datetime import datetime, timedelta

    from agent_factory.staging_manager import StagingManager

    db_path = tmp_path / "telemetry_429.db"
    seen_prompts: list[str] = []
    attempt_counter = [0]

    def _exec_429_then_finish(worktree: Path, task: dict[str, Any]) -> None:
        attempt_counter[0] += 1
        seen_prompts.append(str(task["prompt"]))
        if attempt_counter[0] == 1:
            # Dirty edit in worktree before 429 occurs
            (worktree / "partial_feature.txt").write_text("avance parcial importante\n", encoding="utf-8")
            raise RuntimeError("RESOURCE_EXHAUSTED HTTP 429 Resets in 25m0s")
        # Second attempt (auto-resumed) sees preserved wip file + diff in prompt and completes
        assert (worktree / "partial_feature.txt").read_text(encoding="utf-8") == "avance parcial importante\n"
        (worktree / "final_feature.txt").write_text("completado\n", encoding="utf-8")
        assert _git(worktree, "add", "final_feature.txt").returncode == 0
        assert _git(worktree, "commit", "-m", str(task["title"])).returncode == 0

    manager = StagingManager(
        repo_root=temp_git_repo,
        executor=_exec_429_then_finish,
        db_path=db_path,
    )
    try:
        manager.enqueue_improvement(
            title="feat: resiliente a 429",
            prompt="Crea feature completo sin perder avance.",
        )
        assert manager.wait_until_idle(timeout=15.0) is True

        status_paused = manager.get_staging_status()
        assert status_paused["paused_count"] == 1
        assert status_paused["last_error"] is not None
        assert "⏳ Sin cuota disponible por ahora" in status_paused["last_error"]
        wt = manager.worktree_path
        assert "wip(quota-pause): avance parcial" in _git(wt, "log", "--format=%s", "-n", "1").stdout

        # Expire timer deterministically and wake worker so it resumes with previous git diff
        with manager._cond:
            manager._paused_tasks[0]["resume_at_dt"] = datetime.now().astimezone() - timedelta(seconds=5)
            manager._cond.notify_all()

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            st = manager.get_staging_status()
            if st["paused_count"] == 0 and not st["is_building"] and len(st["commits"]) >= 2:
                break
            time.sleep(0.05)

        final_status = manager.get_staging_status()
        assert final_status["paused_count"] == 0
        assert attempt_counter[0] == 2
        assert "wip(quota-pause)" in seen_prompts[1]
        assert "partial_feature.txt" in seen_prompts[1]
    finally:
        manager.shutdown()


def test_prune_stale_worktrees_cleans_orphans_and_preserves_main_and_active(
    temp_git_repo: Path,
    tmp_path: Path,
) -> None:
    """prune_stale_worktrees prunes expired orphan staging worktrees/branches while preserving main, active staging, and unrelated worktrees/branches."""
    manager = StagingManager(repo_root=temp_git_repo)
    old_ts = int(time.time()) - 7200
    old_env = {
        **os.environ,
        "GIT_AUTHOR_DATE": f"{old_ts} +0000",
        "GIT_COMMITTER_DATE": f"{old_ts} +0000",
    }

    def _git_old(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=old_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    try:
        active_wt = manager.ensure_worktree()
        main_sha_before = _git(temp_git_repo, "rev-parse", "main").stdout.strip()

        # 1. Expired temporary staging worktree + expired temporary branch
        stale_wt = temp_git_repo / ".liwi-staging-tmp-expired"
        assert (
            _git(
                temp_git_repo,
                "worktree",
                "add",
                "-b",
                "liwi-staging-tmp-expired",
                str(stale_wt),
                "main",
            ).returncode
            == 0
        )
        (stale_wt / "stale.txt").write_text("stale\n", encoding="utf-8")
        assert _git(stale_wt, "add", "stale.txt").returncode == 0
        assert _git_old(stale_wt, "commit", "-m", "chore: old stale commit").returncode == 0
        os.utime(stale_wt, (old_ts, old_ts))

        # 2. Fresh temporary staging worktree (must NOT be pruned even if base commit is old)
        fresh_wt = temp_git_repo / ".liwi-staging-tmp-fresh"
        assert (
            _git(
                temp_git_repo,
                "worktree",
                "add",
                "-b",
                "liwi-staging-tmp-fresh",
                str(fresh_wt),
                "main",
            ).returncode
            == 0
        )

        # 3. Unrelated user worktree with old mtime (must NEVER be pruned)
        unrelated_wt = tmp_path / "user_feature_wt"
        assert (
            _git(
                temp_git_repo,
                "worktree",
                "add",
                "-b",
                "feat/user-feature",
                str(unrelated_wt),
                "main",
            ).returncode
            == 0
        )
        os.utime(unrelated_wt, (old_ts, old_ts))

        # 4. Prunable worktree entry whose directory was already removed externally
        ghost_wt = temp_git_repo / ".liwi-staging-ghost"
        assert (
            _git(
                temp_git_repo,
                "worktree",
                "add",
                "-b",
                "liwi-staging-ghost",
                str(ghost_wt),
                "main",
            ).returncode
            == 0
        )
        (ghost_wt / "ghost.txt").write_text("ghost\n", encoding="utf-8")
        assert _git(ghost_wt, "add", "ghost.txt").returncode == 0
        assert _git_old(ghost_wt, "commit", "-m", "chore: ghost commit").returncode == 0
        shutil.rmtree(ghost_wt)

        # 5. Unregistered expired orphan directory inside repo_root
        orphan_dir = temp_git_repo / ".liwi-staging-orphan-dir"
        orphan_dir.mkdir()
        (orphan_dir / "leftover.txt").write_text("leftover\n", encoding="utf-8")
        os.utime(orphan_dir, (old_ts, old_ts))

        # 6. Expired orphan temporary branch (no worktree) + fresh temporary branch + old unrelated branch
        assert _git(temp_git_repo, "checkout", "-b", "liwi-staging-orphan-old", "main").returncode == 0
        (temp_git_repo / "orphan_old.txt").write_text("old\n", encoding="utf-8")
        assert _git(temp_git_repo, "add", "orphan_old.txt").returncode == 0
        assert _git_old(temp_git_repo, "commit", "-m", "chore: orphan old branch").returncode == 0

        assert _git(temp_git_repo, "checkout", "-b", "liwi-staging-fresh-branch", "main").returncode == 0
        (temp_git_repo / "fresh_b.txt").write_text("fresh\n", encoding="utf-8")
        assert _git(temp_git_repo, "add", "fresh_b.txt").returncode == 0
        assert _git(temp_git_repo, "commit", "-m", "chore: fresh temp branch").returncode == 0

        assert _git(temp_git_repo, "checkout", "-b", "feat/unrelated-old", "main").returncode == 0
        (temp_git_repo / "unrelated.txt").write_text("keep\n", encoding="utf-8")
        assert _git(temp_git_repo, "add", "unrelated.txt").returncode == 0
        assert _git_old(temp_git_repo, "commit", "-m", "chore: keep user branch").returncode == 0

        assert _git(temp_git_repo, "checkout", "main").returncode == 0

        summary = manager.prune_stale_worktrees(max_age_seconds=3600)

        assert set(summary.keys()) == {
            "pruned_worktrees",
            "pruned_branches",
            "skipped",
            "errors",
        }
        assert summary["errors"] == []

        # Expired orphan worktrees and directories were removed
        assert not stale_wt.exists()
        assert not orphan_dir.exists()
        assert str(stale_wt.resolve()) in summary["pruned_worktrees"]
        assert str(orphan_dir.resolve()) in summary["pruned_worktrees"]
        assert str(ghost_wt.resolve()) in summary["pruned_worktrees"]

        # Expired orphan temporary branches were removed
        assert "liwi-staging-tmp-expired" in summary["pruned_branches"]
        assert "liwi-staging-ghost" in summary["pruned_branches"]
        assert "liwi-staging-orphan-old" in summary["pruned_branches"]

        # Main repo, main branch, active staging worktree & branch, fresh temp, and user items are intact
        assert temp_git_repo.is_dir()
        assert _git(temp_git_repo, "rev-parse", "main").stdout.strip() == main_sha_before
        assert active_wt.is_dir()
        assert fresh_wt.is_dir()
        assert unrelated_wt.is_dir()

        branches = set(
            _git(temp_git_repo, "for-each-ref", "--format=%(refname:short)", "refs/heads")
            .stdout.splitlines()
        )
        assert "main" in branches
        assert "liwi-staging" in branches
        assert "liwi-staging-tmp-fresh" in branches
        assert "liwi-staging-fresh-branch" in branches
        assert "feat/user-feature" in branches
        assert "feat/unrelated-old" in branches
        assert "liwi-staging-tmp-expired" not in branches
        assert "liwi-staging-ghost" not in branches
        assert "liwi-staging-orphan-old" not in branches

        assert str(temp_git_repo.resolve()) in summary["skipped"]
        assert str(active_wt.resolve()) in summary["skipped"]
        assert str(fresh_wt.resolve()) in summary["skipped"]
        assert str(unrelated_wt.resolve()) in summary["skipped"]
        assert "branch:main" in summary["skipped"]
        assert "branch:liwi-staging" in summary["skipped"]
    finally:
        manager.shutdown()


def test_prune_stale_worktrees_never_deletes_main_locked_or_busy_worktrees_with_mocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With mocked git commands and max_age_seconds=0, main, active staging, locked worktrees, and busy runs are never removed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    active_wt = repo / ".liwi-staging"
    active_wt.mkdir()
    locked_wt = repo / ".liwi-staging-tmp-locked"
    locked_wt.mkdir()
    old_ts = int(time.time()) - 10000
    os.utime(active_wt, (old_ts, old_ts))
    os.utime(locked_wt, (old_ts, old_ts))

    manager = StagingManager(repo_root=repo)
    called_args: list[list[str]] = []

    porcelain_out = (
        f"worktree {repo.resolve()}\n"
        "HEAD 1111111\n"
        "branch refs/heads/main\n\n"
        f"worktree {active_wt.resolve()}\n"
        "HEAD 2222222\n"
        "branch refs/heads/liwi-staging\n\n"
        f"worktree {locked_wt.resolve()}\n"
        "HEAD 3333333\n"
        "branch refs/heads/liwi-staging-tmp-locked\n"
        "locked in progress\n"
    )
    refs_out = (
        f"main\t{old_ts}\n"
        f"liwi-staging\t{old_ts}\n"
        f"liwi-staging-tmp-locked\t{old_ts}\n"
        f"af-par-deadbeef-1\t{old_ts}\n"
    )

    def _fake_run_git(
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: float = 30.0,
    ) -> subprocess.CompletedProcess[str]:
        called_args.append(list(args))
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")
        if args == ["worktree", "list", "--porcelain"]:
            return subprocess.CompletedProcess(args, 0, stdout=porcelain_out, stderr="")
        if args == ["worktree", "prune"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:1] == ["for-each-ref"]:
            return subprocess.CompletedProcess(args, 0, stdout=refs_out, stderr="")
        if args[:2] == ["branch", "-D"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_run_git", _fake_run_git)

    try:
        summary = manager.prune_stale_worktrees(max_age_seconds=0)
        assert ["worktree", "prune"] in called_args
        assert not any(cmd[:2] == ["worktree", "remove"] for cmd in called_args)
        assert ["branch", "-D", "af-par-deadbeef-1"] in called_args
        assert ["branch", "-D", "main"] not in called_args
        assert ["branch", "-D", "liwi-staging"] not in called_args
        assert ["branch", "-D", "liwi-staging-tmp-locked"] not in called_args
        assert summary["pruned_worktrees"] == []
        assert summary["pruned_branches"] == ["af-par-deadbeef-1"]
        assert summary["errors"] == []

        # When manager is actively building, even expired temp branches are skipped
        called_args.clear()
        with manager._lock:
            manager._is_building = True
        try:
            busy_summary = manager.prune_stale_worktrees(max_age_seconds=0)
        finally:
            with manager._lock:
                manager._is_building = False

        assert ["worktree", "prune"] in called_args
        assert not any(cmd[:2] == ["worktree", "remove"] for cmd in called_args)
        assert not any(cmd[:2] == ["branch", "-D"] for cmd in called_args)
        assert busy_summary["pruned_worktrees"] == []
        assert busy_summary["pruned_branches"] == []
        assert busy_summary["errors"] == []
    finally:
        manager.shutdown()


def test_prune_stale_worktrees_handles_subprocess_errors_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subprocess timeouts, OS errors, and non-zero exits are captured in errors without breaking flow."""
    repo = tmp_path / "repo_err"
    repo.mkdir()
    stale_wt = repo / ".liwi-staging-tmp-broken"
    stale_wt.mkdir()
    old_ts = int(time.time()) - 9000
    os.utime(stale_wt, (old_ts, old_ts))

    manager = StagingManager(repo_root=repo)

    porcelain_out = (
        f"worktree {repo.resolve()}\n"
        "HEAD 1111111\n"
        "branch refs/heads/main\n\n"
        f"worktree {stale_wt.resolve()}\n"
        "HEAD 2222222\n"
        "branch refs/heads/liwi-staging-tmp-broken\n"
    )
    refs_out = (
        f"main\t{old_ts}\n"
        f"liwi-staging-tmp-broken\t{old_ts}\n"
        f"liwi-staging-orphan-fail\t{old_ts}\n"
        f"liwi-staging-orphan-ok\t{old_ts}\n"
    )

    def _flaky_run_git(
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: float = 30.0,
    ) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")
        if args == ["worktree", "list", "--porcelain"]:
            return subprocess.CompletedProcess(args, 0, stdout=porcelain_out, stderr="")
        if args == ["worktree", "prune"]:
            raise subprocess.TimeoutExpired(cmd=["git", "worktree", "prune"], timeout=timeout)
        if args[:2] == ["worktree", "remove"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="fatal: cannot remove locked dir")
        if args[:1] == ["for-each-ref"]:
            return subprocess.CompletedProcess(args, 0, stdout=refs_out, stderr="")
        if args == ["branch", "-D", "liwi-staging-orphan-fail"]:
            raise OSError("permission denied executing git")
        if args == ["branch", "-D", "liwi-staging-orphan-ok"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_run_git", _flaky_run_git)

    try:
        summary = manager.prune_stale_worktrees(max_age_seconds=3600)
        assert summary["pruned_worktrees"] == []
        # Branch whose worktree removal failed was NOT deleted; unrelated valid orphan branch WAS deleted
        assert summary["pruned_branches"] == ["liwi-staging-orphan-ok"]
        assert "branch:liwi-staging-tmp-broken" in summary["skipped"]
        assert len(summary["errors"]) == 3
        joined_errors = "\n".join(summary["errors"])
        assert "worktree prune" in joined_errors
        assert "cannot remove locked dir" in joined_errors
        assert "permission denied executing git" in joined_errors

        # If git worktree list fails, do not attempt branch deletions (cannot verify active worktrees)
        def _failing_list_git(
            args: list[str],
            *,
            cwd: Path | None = None,
            timeout: float = 30.0,
        ) -> subprocess.CompletedProcess[str]:
            if args == ["worktree", "list", "--porcelain"]:
                return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: not a git repository")
            if args[:2] == ["branch", "-D"]:
                raise AssertionError("branch -D must not be called when worktree list fails")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(manager, "_run_git", _failing_list_git)
        fail_summary = manager.prune_stale_worktrees(max_age_seconds=0)
        assert fail_summary["pruned_worktrees"] == []
        assert fail_summary["pruned_branches"] == []
        assert any("not a git repository" in e for e in fail_summary["errors"])
    finally:
        manager.shutdown()


def test_validate_diff_scope_accepts_valid_diffs_and_exact_limit() -> None:
    ok, reason = validate_diff_scope(
        [
            "src/agent_factory/staging_manager.py",
            "tests/test_staging_manager.py",
            "src/agent_factory/data_utils.py",
            "database/migrations.py",
        ],
        forbidden_prefixes=DEFAULT_FORBIDDEN_DIFF_PREFIXES,
        max_files=12,
    )
    assert ok is True
    assert reason == ""

    exact_limit_paths = [f"src/agent_factory/mod_{i}.py" for i in range(12)]
    ok_limit, reason_limit = validate_diff_scope(
        exact_limit_paths,
        forbidden_prefixes=DEFAULT_FORBIDDEN_DIFF_PREFIXES,
        max_files=12,
    )
    assert ok_limit is True
    assert reason_limit == ""


def test_validate_diff_scope_rejects_empty_diff() -> None:
    ok_empty, reason_empty = validate_diff_scope(
        [],
        forbidden_prefixes=DEFAULT_FORBIDDEN_DIFF_PREFIXES,
    )
    assert ok_empty is False
    assert "vacío" in reason_empty.lower()

    ok_blank, reason_blank = validate_diff_scope(
        ["", "   ", "."],
        forbidden_prefixes=DEFAULT_FORBIDDEN_DIFF_PREFIXES,
    )
    assert ok_blank is False
    assert "vacío" in reason_blank.lower()


@pytest.mark.parametrize(
    "forbidden_path",
    [
        ".env",
        ".env.local",
        "MASTER_PROMPT.md",
        "AGENTS.md",
        "config/models.toml",
        r"config\models.toml",
        "data/factory.db",
        r"data\nested\factory.db",
        "workspaces/proj_a/app.py",
        r"workspaces\proj_a\app.py",
        "./data/state.json",
        "src/../.env",
    ],
)
def test_validate_diff_scope_rejects_forbidden_paths_with_slash_and_backslash(
    forbidden_path: str,
) -> None:
    ok, reason = validate_diff_scope(
        ["src/agent_factory/ok.py", forbidden_path],
        forbidden_prefixes=DEFAULT_FORBIDDEN_DIFF_PREFIXES,
        max_files=12,
    )
    assert ok is False
    assert "prohibida" in reason.lower()


def test_validate_diff_scope_rejects_excess_files() -> None:
    too_many = [f"src/agent_factory/file_{i}.py" for i in range(13)]
    ok, reason = validate_diff_scope(
        too_many,
        forbidden_prefixes=DEFAULT_FORBIDDEN_DIFF_PREFIXES,
        max_files=12,
    )
    assert ok is False
    assert "13" in reason
    assert "12" in reason

    ok_custom, reason_custom = validate_diff_scope(
        ["a.py", "b.py", "c.py", "d.py"],
        forbidden_prefixes=DEFAULT_FORBIDDEN_DIFF_PREFIXES,
        max_files=3,
    )
    assert ok_custom is False
    assert "4" in reason_custom
    assert "3" in reason_custom


def test_apply_parallel_plan_blocks_pytest_when_diff_scope_fails(
    temp_git_repo: Path,
) -> None:
    manager = StagingManager(repo_root=temp_git_repo)
    pytest_calls: list[list[str]] = []

    def _spy_pytest(tests: list[str], _cwd: Path) -> tuple[bool, str]:
        pytest_calls.append(list(tests))
        return True, "ok"

    try:
        # 1. Empty diff produced by worker -> blocked before pytest runs
        valid_plan = ParallelPlan(
            title="No-op worker plan",
            subtasks=(
                ParallelSubtask(
                    subtask_id="w-1",
                    role="logic",
                    assigned_files=("src/agent_factory/noop_mod.py",),
                ),
            ),
            target_tests=("tests/test_staging_manager.py",),
        )
        res_empty = manager.apply_parallel_plan(
            valid_plan,
            worker_runner=lambda _st, _p, _wt: (True, "nothing written"),
            ruff_runner=lambda _files, _cwd: (True, "ok"),
            pytest_runner=_spy_pytest,
        )
        assert res_empty.success is False
        assert res_empty.commit_allowed is False
        assert res_empty.pytest_ran is False
        assert "vacío" in res_empty.error.lower()
        assert pytest_calls == []

        # 2. Forbidden path in plan -> blocked immediately before workers or pytest
        forbidden_plan = ParallelPlan(
            title="Forbidden path plan",
            subtasks=(
                ParallelSubtask(
                    subtask_id="w-1",
                    role="logic",
                    assigned_files=(r"config\models.toml",),
                ),
            ),
            target_tests=("tests/test_staging_manager.py",),
        )
        worker_calls: list[str] = []
        res_forbidden = manager.apply_parallel_plan(
            forbidden_plan,
            worker_runner=lambda st, _p, _wt: (worker_calls.append(st.subtask_id) or True, "ok"),
            ruff_runner=lambda _files, _cwd: (True, "ok"),
            pytest_runner=_spy_pytest,
        )
        assert res_forbidden.success is False
        assert res_forbidden.commit_allowed is False
        assert res_forbidden.pytest_ran is False
        assert "prohibida" in res_forbidden.error.lower()
        assert worker_calls == []
        assert pytest_calls == []

        # 3. Excess files in plan (> max_files) -> blocked immediately before pytest
        oversized_files = tuple(f"src/agent_factory/mod_{i}.py" for i in range(13))
        oversized_plan = ParallelPlan(
            title="Oversized plan",
            subtasks=(
                ParallelSubtask(
                    subtask_id="w-1",
                    role="logic",
                    assigned_files=oversized_files,
                ),
            ),
            target_tests=("tests/test_staging_manager.py",),
        )
        res_oversized = manager.apply_parallel_plan(
            oversized_plan,
            worker_runner=lambda st, _p, _wt: (worker_calls.append(st.subtask_id) or True, "ok"),
            ruff_runner=lambda _files, _cwd: (True, "ok"),
            pytest_runner=_spy_pytest,
        )
        assert res_oversized.success is False
        assert res_oversized.commit_allowed is False
        assert res_oversized.pytest_ran is False
        assert "13" in res_oversized.error
        assert worker_calls == []
        assert pytest_calls == []
    finally:
        manager.shutdown()


def test_promote_staging_blocks_forbidden_empty_and_oversized_diffs(
    temp_git_repo: Path,
) -> None:
    restart_calls: list[str] = []
    manager = StagingManager(
        repo_root=temp_git_repo,
        on_restart=lambda: restart_calls.append("restarted"),
    )
    initial_main_sha = _git(temp_git_repo, "rev-parse", "main").stdout.strip()

    try:
        wt = manager.ensure_worktree()

        # 1. Empty commit on staging -> rejected before promote
        assert _git(wt, "commit", "--allow-empty", "-m", "chore: empty staging commit").returncode == 0
        with pytest.raises(RuntimeError, match="vacío"):
            manager.promote_staging_to_main(trigger_restart=True)
        assert _git(temp_git_repo, "rev-parse", "main").stdout.strip() == initial_main_sha
        assert restart_calls == []
        assert manager.get_staging_status()["last_error"] is not None

        # Reset staging back to main
        assert _git(wt, "reset", "--hard", initial_main_sha).returncode == 0

        # 2. Forbidden file committed on staging (data/leak.db) -> rejected before promote
        leak_file = wt / "data" / "leak.db"
        leak_file.parent.mkdir(parents=True, exist_ok=True)
        leak_file.write_text("do not promote\n", encoding="utf-8")
        assert _git(wt, "add", "data/leak.db").returncode == 0
        assert _git(wt, "commit", "-m", "chore: touch forbidden data path").returncode == 0

        with pytest.raises(RuntimeError, match="prohibida"):
            manager.promote_staging_to_main(trigger_restart=True)
        assert _git(temp_git_repo, "rev-parse", "main").stdout.strip() == initial_main_sha
        assert restart_calls == []
        assert manager.get_staging_status()["last_error"] is not None

        # Reset staging back to main
        assert _git(wt, "reset", "--hard", initial_main_sha).returncode == 0

        # 3. Too many files committed on staging (13 > 12) -> rejected before promote
        for i in range(13):
            f = wt / f"batch_{i}.txt"
            f.write_text(f"content {i}\n", encoding="utf-8")
        assert _git(wt, "add", "-A").returncode == 0
        assert _git(wt, "commit", "-m", "chore: too many files").returncode == 0

        with pytest.raises(RuntimeError, match="13"):
            manager.promote_staging_to_main(trigger_restart=True)
        assert _git(temp_git_repo, "rev-parse", "main").stdout.strip() == initial_main_sha
        assert restart_calls == []
    finally:
        manager.shutdown()



