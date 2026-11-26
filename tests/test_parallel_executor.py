"""Unit and real-git integration tests for parallel_executor (INV-01, INV-19, INV-22, INV-23)."""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_factory.adapters.agy_cli_adapter import build_sanitized_cli_env
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.domain.models import FileSpec
from agent_factory.parallel_executor import (
    MAX_PARALLEL_AGENTS,
    ParallelExecutionReport,
    ParallelSubtask,
    ParallelWorktreeExecutor,
    SubtaskProgress,
    normalize_owned_path,
    partition_plan_files,
    validate_disjoint_subtasks,
)


def _git(repo_root: Path, *args: str) -> str:
    """Run a local git command in repo_root using a sanitized environment."""
    runner = SubprocessRunner(default_timeout_seconds=30.0)
    env = build_sanitized_cli_env()
    env["GIT_TERMINAL_PROMPT"] = "0"
    res = runner.run(["git", *args], cwd=repo_root, env=env)
    assert res.exit_code == 0, f"git {' '.join(args)} failed: {res.stderr or res.stdout}"
    return res.stdout


def _init_temp_git_repo(repo_root: Path, initial_files: dict[str, str]) -> Path:
    """Create a real local git repository with an initial commit."""
    repo_root.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "init")
    _git(repo_root, "config", "user.name", "Test Author")
    _git(repo_root, "config", "user.email", "test@factory.local")
    for rel_path, content in initial_files.items():
        target = repo_root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "--no-verify", "-m", "chore(test): initial commit")
    return repo_root


def _list_branches(repo_root: Path) -> list[str]:
    out = _git(repo_root, "branch", "--format=%(refname:short)")
    return [line.strip() for line in out.splitlines() if line.strip()]


def _list_worktrees(repo_root: Path) -> list[str]:
    out = _git(repo_root, "worktree", "list", "--porcelain")
    return [
        line.removeprefix("worktree ").strip()
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]


class TestNormalizeOwnedPath:
    """Tests for normalize_owned_path path sanitization and security rules (INV-19, INV-22)."""

    def test_normalizes_clean_relative_paths(self) -> None:
        assert normalize_owned_path("src/app.py") == "src/app.py"
        assert normalize_owned_path("./src/utils/helpers.py") == "src/utils/helpers.py"
        assert normalize_owned_path("src//nested/./file.py") == "src/nested/file.py"

    @pytest.mark.parametrize(
        "bad_path",
        [
            "",
            "   ",
            ".",
            "./",
            "../secret.py",
            "src/../../etc/passwd",
            "/etc/passwd",
            "C:/Windows/win.ini",
            r"src\app.py",
            r"C:\secret\file.txt",
            ".git/config",
            "nested/.git/HEAD",
            ".venv/pyvenv.cfg",
            "venv/bin/python",
            "node_modules/pkg/index.js",
            ".env",
            ".env.local",
            "config/.env.production",
            "certs/private.key",
            "certs/server.pem",
            "src/file\x00.py",
        ],
    )
    def test_rejects_unsafe_paths(self, bad_path: str) -> None:
        with pytest.raises(ValueError):
            normalize_owned_path(bad_path)


class TestValidateDisjointSubtasks:
    """Tests for validate_disjoint_subtasks ownership and protected-path validation."""

    def test_accepts_disjoint_subtasks_and_normalizes_paths(self) -> None:
        subtasks = [
            ParallelSubtask(task_id="t1", label="Module A", owned_files=("./src/a.py",)),
            ParallelSubtask(task_id="t2", label="Module B", owned_files=("src/b.py", "src/b2.py")),
        ]
        validated = validate_disjoint_subtasks(
            subtasks,
            forbidden_prefixes=("MASTER_PROMPT.md", "AGENTS.md", "data"),
        )
        assert len(validated) == 2
        assert validated[0].owned_files == ("src/a.py",)
        assert validated[1].owned_files == ("src/b.py", "src/b2.py")

    def test_rejects_duplicate_file_across_subtasks(self) -> None:
        subtasks = [
            ParallelSubtask(task_id="t1", label="A", owned_files=("src/shared.py", "src/a.py")),
            ParallelSubtask(task_id="t2", label="B", owned_files=("./src/shared.py",)),
        ]
        with pytest.raises(ValueError, match="overlap|solap|duplicate|shared"):
            validate_disjoint_subtasks(subtasks)

    def test_rejects_empty_owned_files(self) -> None:
        subtasks = [
            ParallelSubtask(task_id="t1", label="Empty", owned_files=()),
        ]
        with pytest.raises(ValueError):
            validate_disjoint_subtasks(subtasks)

    def test_rejects_path_traversal(self) -> None:
        subtasks = [
            ParallelSubtask(task_id="t1", label="Escape", owned_files=("../outside.py",)),
        ]
        with pytest.raises(ValueError):
            validate_disjoint_subtasks(subtasks)

    def test_rejects_protected_env_and_forbidden_prefixes(self) -> None:
        env_subtasks = [
            ParallelSubtask(task_id="t1", label="Env", owned_files=(".env",)),
        ]
        with pytest.raises(ValueError):
            validate_disjoint_subtasks(env_subtasks)

        master_prompt_subtasks = [
            ParallelSubtask(task_id="t1", label="Prompt", owned_files=("MASTER_PROMPT.md",)),
        ]
        with pytest.raises(ValueError):
            validate_disjoint_subtasks(
                master_prompt_subtasks,
                forbidden_prefixes=("MASTER_PROMPT.md", "AGENTS.md", "config/models.toml"),
            )

        nested_forbidden = [
            ParallelSubtask(task_id="t1", label="Data", owned_files=("data/agent_factory.db",)),
        ]
        with pytest.raises(ValueError):
            validate_disjoint_subtasks(nested_forbidden, forbidden_prefixes=("data",))

        # Prefix matching must respect path boundaries (data_helper.py is NOT inside data/)
        safe_sibling = [
            ParallelSubtask(task_id="t1", label="Safe", owned_files=("data_helper.py",)),
        ]
        validated = validate_disjoint_subtasks(safe_sibling, forbidden_prefixes=("data",))
        assert validated[0].owned_files == ("data_helper.py",)


class TestPartitionPlanFiles:
    """Tests for connected-component grouping and disjoint partitioning of FileSpecs."""

    def test_independent_files_capped_at_max_parallel_agents(self) -> None:
        assert MAX_PARALLEL_AGENTS == 3
        files = [
            FileSpec(path=f"src/mod_{i}.py", purpose=f"Module {i}", contract=f"Exposes fn_{i}")
            for i in range(6)
        ]
        subtasks = partition_plan_files(files, max_workers=10)
        assert len(subtasks) == 3

        all_owned: list[str] = []
        for st in subtasks:
            assert len(st.owned_files) == 2
            all_owned.extend(st.owned_files)
        assert sorted(all_owned) == [f"src/mod_{i}.py" for i in range(6)]
        assert len(set(all_owned)) == 6

    def test_coupled_files_by_depends_on_stay_in_same_group(self) -> None:
        files = [
            FileSpec(path="src/core.py", purpose="Core", contract="Core types"),
            FileSpec(
                path="src/service.py",
                purpose="Service",
                contract="Business logic",
                depends_on=["src/core.py"],
            ),
            FileSpec(
                path="src/ui.py",
                purpose="UI",
                contract="Render UI",
                depends_on=["src/service.py"],
            ),
            FileSpec(path="src/styles.css", purpose="Styles", contract="Theme CSS"),
            FileSpec(path="src/docs.md", purpose="Docs", contract="Usage docs"),
        ]
        subtasks = partition_plan_files(files, max_workers=3)
        assert len(subtasks) == 3

        groups = [set(st.owned_files) for st in subtasks]
        assert {"src/core.py", "src/service.py", "src/ui.py"} in groups
        assert {"src/styles.css"} in groups
        assert {"src/docs.md"} in groups

    def test_single_connected_group_returns_one_subtask_for_sequential_fallback(self) -> None:
        files = [
            FileSpec(path="src/a.py", purpose="A", contract="A", depends_on=["src/b.py"]),
            FileSpec(path="src/b.py", purpose="B", contract="B", depends_on=["src/a.py"]),
        ]
        subtasks = partition_plan_files(files, max_workers=3)
        assert len(subtasks) == 1
        assert set(subtasks[0].owned_files) == {"src/a.py", "src/b.py"}

    def test_empty_files_returns_empty_list(self) -> None:
        assert partition_plan_files([], max_workers=3) == []


class TestSubtaskProgress:
    """Tests for SubtaskProgress formatting."""

    def test_readable_progress_format(self) -> None:
        prog = SubtaskProgress(
            agent_index=1,
            total_agents=3,
            task_id="subtask-1",
            label="Frontend UI",
            owned_files=("src/ui.js",),
            status="running",
            detail="Writing components",
        )
        assert prog.agent_tag == "Agente 1/3"
        formatted = prog.format_line()
        assert "Agente 1/3: Frontend UI (running)" in formatted
        assert "Writing components" in formatted


class TestParallelWorktreeExecutor:
    """Real temporary git repository tests for concurrent worktree execution and merge."""

    def test_concurrent_execution_of_three_agents_merges_cleanly_and_cleans_up(
        self,
        tmp_path: Path,
    ) -> None:
        repo_root = _init_temp_git_repo(
            tmp_path / "repo",
            {
                "README.md": "# Base repo\n",
                "src/a.txt": "base-a\n",
                "src/b.txt": "base-b\n",
                "src/c.txt": "base-c\n",
            },
        )
        initial_branches = _list_branches(repo_root)

        subtasks = [
            ParallelSubtask(task_id="task-a", label="Modulo A", owned_files=("src/a.txt",)),
            ParallelSubtask(task_id="task-b", label="Modulo B", owned_files=("src/b.txt",)),
            ParallelSubtask(task_id="task-c", label="Modulo C", owned_files=("src/c.txt",)),
        ]

        barrier = threading.Barrier(3, timeout=10.0)
        seen_tags: list[str] = []
        tags_lock = threading.Lock()
        progress_summaries: list[str] = []
        progress_snapshots: list[list[SubtaskProgress]] = []

        def worker(subtask: ParallelSubtask, wt_path: Path, agent_tag: str) -> tuple[bool, str]:
            # Proves all 3 workers run concurrently in isolated worktrees
            barrier.wait()
            with tags_lock:
                seen_tags.append(agent_tag)
            target_file = wt_path / subtask.owned_files[0]
            target_file.write_text(f"updated by {agent_tag} ({subtask.task_id})\n", encoding="utf-8")
            return True, f"Updated {subtask.owned_files[0]}"

        def on_progress(snapshot: list[SubtaskProgress], summary: str) -> None:
            progress_snapshots.append(snapshot)
            progress_summaries.append(summary)

        executor = ParallelWorktreeExecutor(repo_root, max_workers=5)
        assert executor.max_workers == MAX_PARALLEL_AGENTS

        report = executor.execute(subtasks, worker, on_progress=on_progress)

        assert isinstance(report, ParallelExecutionReport)
        assert report.succeeded is True
        assert report.error == ""
        assert report.conflict_files == []
        assert report.integration_attempted is False
        assert report.integration_succeeded is False
        assert len(report.subtasks) == 3
        assert len(report.merged_branches) == 3
        assert sorted(seen_tags) == ["Agente 1/3", "Agente 2/3", "Agente 3/3"]

        # Verify progress callbacks reported all three agents
        combined_progress = "\n".join(progress_summaries)
        assert "Agente 1/3: Modulo A" in combined_progress
        assert "Agente 2/3: Modulo B" in combined_progress
        assert "Agente 3/3: Modulo C" in combined_progress
        assert all(p.status == "done" for p in progress_snapshots[-1])

        # Verify changes were merged cleanly into main repo
        assert (repo_root / "src/a.txt").read_text(encoding="utf-8") == (
            "updated by Agente 1/3 (task-a)\n"
        )
        assert (repo_root / "src/b.txt").read_text(encoding="utf-8") == (
            "updated by Agente 2/3 (task-b)\n"
        )
        assert (repo_root / "src/c.txt").read_text(encoding="utf-8") == (
            "updated by Agente 3/3 (task-c)\n"
        )

        # Verify ALL temporary worktrees and branches were cleaned up
        assert _list_branches(repo_root) == initial_branches
        assert len(_list_worktrees(repo_root)) == 1
        assert _git(repo_root, "status", "--porcelain").strip() == ""

    def test_ownership_violation_blocks_merge_and_cleans_up_worktrees_and_branches(
        self,
        tmp_path: Path,
    ) -> None:
        repo_root = _init_temp_git_repo(
            tmp_path / "repo",
            {
                "src/a.txt": "orig-a\n",
                "src/b.txt": "orig-b\n",
                "src/other.txt": "orig-other\n",
            },
        )
        initial_branches = _list_branches(repo_root)

        subtasks = [
            ParallelSubtask(task_id="task-a", label="Good Agent", owned_files=("src/a.txt",)),
            ParallelSubtask(task_id="task-b", label="Rogue Agent", owned_files=("src/b.txt",)),
        ]

        def worker(subtask: ParallelSubtask, wt_path: Path, agent_tag: str) -> tuple[bool, str]:
            if subtask.task_id == "task-a":
                (wt_path / "src/a.txt").write_text("changed-a\n", encoding="utf-8")
                return True, "ok"
            # Rogue agent modifies its own file AND a file outside its owned_files
            (wt_path / "src/b.txt").write_text("changed-b\n", encoding="utf-8")
            (wt_path / "src/other.txt").write_text("hijacked!\n", encoding="utf-8")
            return True, "sneaky"

        executor = ParallelWorktreeExecutor(
            repo_root,
            max_workers=2,
            secrets_to_redact=("super_secret_token_999",),
        )
        report = executor.execute(subtasks, worker)

        assert report.succeeded is False
        assert report.merged_branches == []
        assert report.subtasks[0].succeeded is True
        assert report.subtasks[1].succeeded is False
        assert "src/other.txt" in report.subtasks[1].error

        # Nothing merged into main repo
        assert (repo_root / "src/a.txt").read_text(encoding="utf-8") == "orig-a\n"
        assert (repo_root / "src/b.txt").read_text(encoding="utf-8") == "orig-b\n"
        assert (repo_root / "src/other.txt").read_text(encoding="utf-8") == "orig-other\n"

        # Worktrees and temporary branches cleaned up
        assert _list_branches(repo_root) == initial_branches
        assert len(_list_worktrees(repo_root)) == 1
        assert _git(repo_root, "status", "--porcelain").strip() == ""

    def test_merge_conflict_resolved_in_single_integration_round(self, tmp_path: Path) -> None:
        repo_root = _init_temp_git_repo(
            tmp_path / "repo",
            {
                "src/shared.txt": "line-1\nbase-value\n",
                "src/other.txt": "other-base\n",
            },
        )
        initial_branches = _list_branches(repo_root)

        subtasks = [
            ParallelSubtask(task_id="task-1", label="Shared Edit", owned_files=("src/shared.txt",)),
            ParallelSubtask(task_id="task-2", label="Other Edit", owned_files=("src/other.txt",)),
        ]

        main_updated = threading.Event()

        def worker(subtask: ParallelSubtask, wt_path: Path, agent_tag: str) -> tuple[bool, str]:
            if subtask.task_id == "task-1":
                # Simulate a concurrent commit landing on repo_root while worktree runs
                (repo_root / "src/shared.txt").write_text("line-1\nmain-value\n", encoding="utf-8")
                _git(repo_root, "add", "src/shared.txt")
                _git(repo_root, "commit", "--no-verify", "-m", "chore(test): competing main edit")
                main_updated.set()
                (wt_path / "src/shared.txt").write_text(
                    "line-1\nworktree-value\n",
                    encoding="utf-8",
                )
                return True, "Updated shared.txt"
            main_updated.wait(timeout=5.0)
            (wt_path / "src/other.txt").write_text("other-updated\n", encoding="utf-8")
            return True, "Updated other.txt"

        resolver_calls: list[tuple[list[str], str]] = []

        def resolver(
            root: Path,
            conflict_files: Sequence[str],
            merge_error: str,
        ) -> tuple[bool, str]:
            resolver_calls.append((list(conflict_files), merge_error))
            for rel in conflict_files:
                (root / rel).write_text("line-1\nresolved-value\n", encoding="utf-8")
            return True, "Resolved conflict cleanly"

        executor = ParallelWorktreeExecutor(repo_root, max_workers=2)
        report = executor.execute(subtasks, worker, conflict_resolver_fn=resolver)

        assert report.succeeded is True
        assert report.integration_attempted is True
        assert report.integration_succeeded is True
        assert report.conflict_files == []
        assert len(report.merged_branches) == 2
        assert len(resolver_calls) == 1
        assert resolver_calls[0][0] == ["src/shared.txt"]

        assert (repo_root / "src/shared.txt").read_text(encoding="utf-8") == (
            "line-1\nresolved-value\n"
        )
        assert (repo_root / "src/other.txt").read_text(encoding="utf-8") == "other-updated\n"
        assert _list_branches(repo_root) == initial_branches
        assert len(_list_worktrees(repo_root)) == 1
        assert _git(repo_root, "status", "--porcelain").strip() == ""

    def test_merge_conflict_without_resolver_aborts_cleanly_and_leaves_repo_healthy(
        self,
        tmp_path: Path,
    ) -> None:
        repo_root = _init_temp_git_repo(
            tmp_path / "repo",
            {
                "src/shared.txt": "base\n",
            },
        )
        initial_branches = _list_branches(repo_root)

        subtasks = [
            ParallelSubtask(task_id="task-1", label="Conflicting Edit", owned_files=("src/shared.txt",)),
        ]

        def worker(subtask: ParallelSubtask, wt_path: Path, agent_tag: str) -> tuple[bool, str]:
            # Advance main with conflicting content
            (repo_root / "src/shared.txt").write_text("main-conflict\n", encoding="utf-8")
            _git(repo_root, "add", "src/shared.txt")
            _git(repo_root, "commit", "--no-verify", "-m", "chore(test): conflicting main commit")
            # Write conflicting content in worktree
            (wt_path / "src/shared.txt").write_text("branch-conflict\n", encoding="utf-8")
            return True, "Updated shared.txt"

        executor = ParallelWorktreeExecutor(repo_root, max_workers=1)
        report = executor.execute(subtasks, worker, conflict_resolver_fn=None)

        assert report.succeeded is False
        assert report.integration_attempted is False
        assert report.integration_succeeded is False
        assert report.conflict_files == ["src/shared.txt"]
        assert report.merged_branches == []

        # Repository must be left clean (merge aborted, no MERGE_HEAD, original main content intact)
        assert not (repo_root / ".git" / "MERGE_HEAD").exists()
        assert (repo_root / "src/shared.txt").read_text(encoding="utf-8") == "main-conflict\n"
        assert _git(repo_root, "status", "--porcelain").strip() == ""
        assert _list_branches(repo_root) == initial_branches
        assert len(_list_worktrees(repo_root)) == 1

    def test_merge_conflict_failed_resolver_aborts_cleanly_and_caps_at_one_round(
        self,
        tmp_path: Path,
    ) -> None:
        repo_root = _init_temp_git_repo(
            tmp_path / "repo",
            {
                "src/shared.txt": "base\n",
            },
        )
        initial_branches = _list_branches(repo_root)
        subtasks = [
            ParallelSubtask(task_id="task-1", label="Conflicting Edit", owned_files=("src/shared.txt",)),
        ]

        def worker(subtask: ParallelSubtask, wt_path: Path, agent_tag: str) -> tuple[bool, str]:
            (repo_root / "src/shared.txt").write_text("main-v2\n", encoding="utf-8")
            _git(repo_root, "add", "src/shared.txt")
            _git(repo_root, "commit", "--no-verify", "-m", "chore(test): competing main v2")
            (wt_path / "src/shared.txt").write_text("branch-v2\n", encoding="utf-8")
            return True, "ok"

        calls = 0

        def failing_resolver(
            root: Path,
            conflict_files: Sequence[str],
            merge_error: str,
        ) -> tuple[bool, str]:
            nonlocal calls
            calls += 1
            return False, "Could not resolve conflict with secret_abc_999"

        executor = ParallelWorktreeExecutor(
            repo_root,
            max_workers=1,
            secrets_to_redact=("secret_abc_999",),
        )
        report = executor.execute(subtasks, worker, conflict_resolver_fn=failing_resolver)

        assert calls == 1
        assert report.succeeded is False
        assert report.integration_attempted is True
        assert report.integration_succeeded is False
        assert report.conflict_files == ["src/shared.txt"]
        assert "secret_abc_999" not in report.error
        assert "[REDACTED]" in report.error
        assert not (repo_root / ".git" / "MERGE_HEAD").exists()
        assert (repo_root / "src/shared.txt").read_text(encoding="utf-8") == "main-v2\n"
        assert _git(repo_root, "status", "--porcelain").strip() == ""
        assert _list_branches(repo_root) == initial_branches
        assert len(_list_worktrees(repo_root)) == 1

    def test_error_messages_redact_secrets(self, tmp_path: Path) -> None:
        repo_root = _init_temp_git_repo(
            tmp_path / "repo",
            {"src/a.txt": "base\n"},
        )
        subtasks = [
            ParallelSubtask(task_id="task-a", label="Fail With Secret", owned_files=("src/a.txt",)),
        ]

        def failing_worker(subtask: ParallelSubtask, wt_path: Path, agent_tag: str) -> tuple[bool, str]:
            time.sleep(0.01)
            raise RuntimeError("boom with top_secret_xyz123 and ghp_abcdefghijklmnopqrstuvwxyz123456")

        executor = ParallelWorktreeExecutor(
            repo_root,
            max_workers=1,
            secrets_to_redact=("top_secret_xyz123",),
        )
        report = executor.execute(subtasks, failing_worker)

        assert report.succeeded is False
        assert "top_secret_xyz123" not in report.error
        assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in report.error
        assert "[REDACTED]" in report.error
        assert "top_secret_xyz123" not in report.subtasks[0].error
        assert "[REDACTED]" in report.subtasks[0].error
        assert len(_list_worktrees(repo_root)) == 1
