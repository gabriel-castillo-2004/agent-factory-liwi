"""Targeted TDD unit tests for ParallelImprover and StagingManager parallel plan integration."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_factory.parallel_improver import (
    MAX_PARALLEL_WORKERS,
    ModuleContract,
    ParallelImprover,
    ParallelPlan,
    ParallelRunResult,
    ParallelSubtask,
    WorkerResult,
    build_disjoint_plan,
    build_worker_prompt,
    generate_module_contracts,
    normalize_relative_path,
    validate_disjoint_subtasks,
)
from agent_factory.staging_manager import StagingManager


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

    (repo / "src" / "agent_factory").mkdir(parents=True, exist_ok=True)
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "agent_factory" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "README.md").write_text("# Agent Factory Test Repo\n", encoding="utf-8")
    assert _git(repo, "add", "-A").returncode == 0
    assert _git(repo, "commit", "-m", "chore: initial commit").returncode == 0
    return repo


def test_build_disjoint_plan_partitions_logic_ui_tests_and_propagates_contracts() -> None:
    contracts = (
        ModuleContract(
            module="agent_factory.feature_logic",
            symbols=("compute_score(items: list[str]) -> int",),
            description="Computes deterministic feature score.",
        ),
        ModuleContract(
            module="agent_factory.dashboard",
            symbols=("render_feature_panel(score: int) -> str",),
            description="Renders score panel without external CDNs.",
        ),
    )

    plan = build_disjoint_plan(
        title="Add feature scoring pipeline",
        files=[
            "src/agent_factory/feature_logic.py",
            "src\\agent_factory\\dashboard.py",
            "tests/test_feature_logic.py",
        ],
        prompt="Implement feature scoring across logic, UI, and tests.",
        contracts=contracts,
    )

    assert isinstance(plan, ParallelPlan)
    assert plan.title == "Add feature scoring pipeline"
    assert len(plan.subtasks) == 3

    by_role = {st.role: st for st in plan.subtasks}
    assert set(by_role) == {"logic", "ui", "tests"}
    assert by_role["logic"].assigned_files == ("src/agent_factory/feature_logic.py",)
    assert by_role["ui"].assigned_files == ("src/agent_factory/dashboard.py",)
    assert by_role["tests"].assigned_files == ("tests/test_feature_logic.py",)
    assert plan.target_tests == ("tests/test_feature_logic.py",)

    # Contracts are propagated to every subtask and rendered in worker prompts
    for subtask in plan.subtasks:
        assert subtask.contracts == contracts
        worker_prompt = build_worker_prompt(plan, subtask)
        assert "agent_factory.feature_logic" in worker_prompt
        assert "compute_score(items: list[str]) -> int" in worker_prompt
        assert "render_feature_panel(score: int) -> str" in worker_prompt
        for assigned in subtask.assigned_files:
            assert assigned in worker_prompt
        assert "fuera de tu partición" in worker_prompt.lower() or "fuera de su partición" in worker_prompt.lower()
        assert "pytest" in worker_prompt.lower()


def test_build_disjoint_plan_generates_default_contracts_when_omitted() -> None:
    generated = generate_module_contracts(
        [
            "src/agent_factory/alpha_worker.py",
            "src/agent_factory/dashboard.py",
            "tests/test_alpha_worker.py",
        ]
    )
    assert [c.module for c in generated] == [
        "agent_factory.alpha_worker",
        "agent_factory.dashboard",
    ]

    plan = build_disjoint_plan(
        title="Auto contracts",
        files=[
            "src/agent_factory/alpha_worker.py",
            "src/agent_factory/dashboard.py",
            "tests/test_alpha_worker.py",
        ],
    )
    assert [c.module for c in plan.contracts] == [
        "agent_factory.alpha_worker",
        "agent_factory.dashboard",
    ]
    assert all(st.contracts == plan.contracts for st in plan.subtasks)


def test_build_disjoint_plan_single_file_or_single_partition_returns_one_subtask() -> None:
    single_file_plan = build_disjoint_plan(
        title="Single file tweak",
        files=["src/agent_factory/only_one.py"],
        prompt="Small refactor.",
    )
    assert len(single_file_plan.subtasks) == 1
    assert single_file_plan.subtasks[0].role == "logic"
    assert single_file_plan.subtasks[0].assigned_files == ("src/agent_factory/only_one.py",)

    single_partition_plan = build_disjoint_plan(
        title="Two logic files only",
        files=[
            "src/agent_factory/mod_a.py",
            "src/agent_factory/mod_b.py",
        ],
        prompt="Refactor two logic modules together.",
    )
    assert len(single_partition_plan.subtasks) == 1
    assert single_partition_plan.subtasks[0].role == "logic"
    assert single_partition_plan.subtasks[0].assigned_files == (
        "src/agent_factory/mod_a.py",
        "src/agent_factory/mod_b.py",
    )


def test_validate_disjoint_subtasks_rejects_overlapping_paths_across_separators() -> None:
    st1 = ParallelSubtask(
        subtask_id="worker-logic",
        role="logic",
        assigned_files=("src/a.py",),
        instructions="Update a.py",
    )
    st2 = ParallelSubtask(
        subtask_id="worker-ui",
        role="ui",
        assigned_files=("src\\a.py",),
        instructions="Also touch a.py",
    )

    with pytest.raises(ValueError, match="src/a.py"):
        validate_disjoint_subtasks([st1, st2])

    st_dot = ParallelSubtask(
        subtask_id="worker-dot",
        role="tests",
        assigned_files=("./src/a.py",),
        instructions="Touch a.py with dot prefix",
    )
    with pytest.raises(ValueError, match="src/a.py"):
        validate_disjoint_subtasks([st1, st_dot])

    if os.name == "nt":
        st_case = ParallelSubtask(
            subtask_id="worker-case",
            role="ui",
            assigned_files=("SRC\\A.PY",),
            instructions="Windows case collision",
        )
        with pytest.raises(ValueError, match="(?i)src/a.py"):
            validate_disjoint_subtasks([st1, st_case])


@pytest.mark.parametrize(
    "bad_path",
    [
        "",
        "../outside.py",
        "/etc/passwd",
        "C:\\Windows\\system.ini",
        ".env",
        "MASTER_PROMPT.md",
        "AGENTS.md",
        "config/models.toml",
        "data/factory.db",
        "workspaces/demo/app.py",
    ],
)
def test_normalize_relative_path_rejects_unsafe_or_forbidden_paths(bad_path: str) -> None:
    with pytest.raises(ValueError):
        normalize_relative_path(bad_path)


def test_parallel_improver_runs_concurrently_and_caps_at_three_workers(tmp_path: Path) -> None:
    assert MAX_PARALLEL_WORKERS == 3

    subtasks = tuple(
        ParallelSubtask(
            subtask_id=f"w-{idx}",
            role=f"part-{idx}",
            assigned_files=(f"src/agent_factory/mod_{idx}.py",),
            instructions=f"Implement mod_{idx}.py",
        )
        for idx in range(1, 6)
    )
    plan = build_disjoint_plan(
        title="Five disjoint tasks capped at 3 concurrent workers",
        subtasks=subtasks,
        target_tests=("tests/test_parallel_improver.py",),
    )

    lock = threading.Lock()
    active = 0
    max_observed_active = 0
    barrier = threading.Barrier(3, timeout=5.0)
    barrier_reached = threading.Event()
    ruff_calls: list[list[str]] = []
    pytest_calls: list[list[str]] = []

    def fake_worker(subtask: ParallelSubtask, _prompt: str, _cwd: Path) -> tuple[bool, str]:
        nonlocal active, max_observed_active
        with lock:
            active += 1
            max_observed_active = max(max_observed_active, active)
        try:
            if subtask.subtask_id in {"w-1", "w-2", "w-3"}:
                barrier.wait()
                barrier_reached.set()
            else:
                time.sleep(0.03)
            return True, f"done {subtask.subtask_id}"
        finally:
            with lock:
                active -= 1

    def fake_ruff(files: Sequence[str], _cwd: Path) -> tuple[bool, str]:
        with lock:
            ruff_calls.append(list(files))
        return True, "All checks passed"

    def fake_pytest(tests: Sequence[str], _cwd: Path) -> tuple[bool, str]:
        with lock:
            pytest_calls.append(list(tests))
        return True, "1 passed"

    # Requesting max_workers=8 must still be strictly capped to 3
    improver = ParallelImprover(
        worktree_path=tmp_path,
        max_workers=8,
        worker_runner=fake_worker,
        ruff_runner=fake_ruff,
        pytest_runner=fake_pytest,
    )
    assert improver.max_workers == 3

    result = improver.run(plan)
    assert isinstance(result, ParallelRunResult)
    assert result.success is True
    assert result.commit_allowed is True
    assert barrier_reached.is_set()
    assert max_observed_active == 3
    assert len(result.worker_results) == 5
    assert all(isinstance(wr, WorkerResult) and wr.success and wr.ruff_ok for wr in result.worker_results)

    # Each worker ran ruff ONLY on its own assigned file
    assert sorted(ruff_calls) == [
        ["src/agent_factory/mod_1.py"],
        ["src/agent_factory/mod_2.py"],
        ["src/agent_factory/mod_3.py"],
        ["src/agent_factory/mod_4.py"],
        ["src/agent_factory/mod_5.py"],
    ]
    # Targeted pytest ran ONCE at the very end
    assert pytest_calls == [["tests/test_parallel_improver.py"]]
    assert result.pytest_ran is True
    assert result.pytest_ok is True


def test_parallel_improver_blocks_commit_and_skips_pytest_on_worker_or_ruff_failure(
    tmp_path: Path,
) -> None:
    plan = build_disjoint_plan(
        title="Worker failure case",
        files=[
            "src/agent_factory/mod_ok.py",
            "src/agent_factory/dashboard.py",
            "tests/test_mod_ok.py",
        ],
    )

    pytest_calls: list[list[str]] = []

    def failing_worker(subtask: ParallelSubtask, _prompt: str, _cwd: Path) -> tuple[bool, str]:
        if subtask.role == "ui":
            return False, "simulated agy worker failure"
        return True, "ok"

    improver = ParallelImprover(
        worktree_path=tmp_path,
        worker_runner=failing_worker,
        ruff_runner=lambda files, _cwd: (True, f"linted {files}"),
        pytest_runner=lambda tests, _cwd: (pytest_calls.append(list(tests)) or True, "ok"),
    )
    res = improver.run(plan)
    assert res.success is False
    assert res.commit_allowed is False
    assert res.pytest_ran is False
    assert pytest_calls == []
    assert "simulated agy worker failure" in res.error

    # Now test ruff failure on one worker
    ruff_pytest_calls: list[list[str]] = []

    def failing_ruff(files: Sequence[str], _cwd: Path) -> tuple[bool, str]:
        if "src/agent_factory/dashboard.py" in files:
            return False, "F401 unused import"
        return True, "ok"

    improver_ruff_fail = ParallelImprover(
        worktree_path=tmp_path,
        worker_runner=lambda _st, _p, _cwd: (True, "ok"),
        ruff_runner=failing_ruff,
        pytest_runner=lambda tests, _cwd: (ruff_pytest_calls.append(list(tests)) or True, "ok"),
    )
    res_ruff = improver_ruff_fail.run(plan)
    assert res_ruff.success is False
    assert res_ruff.commit_allowed is False
    assert res_ruff.pytest_ran is False
    assert ruff_pytest_calls == []
    assert "F401" in res_ruff.error


def test_parallel_improver_blocks_commit_on_out_of_partition_modification(tmp_path: Path) -> None:
    plan = build_disjoint_plan(
        title="Out of partition check",
        files=[
            "src/agent_factory/allowed_mod.py",
            "tests/test_allowed_mod.py",
        ],
    )
    pytest_calls: list[list[str]] = []
    inspect_calls = 0

    def inspector(_cwd: Path) -> list[str]:
        nonlocal inspect_calls
        inspect_calls += 1
        if inspect_calls == 1:
            return []
        return ["src/agent_factory/allowed_mod.py", "src/agent_factory/unexpected_other.py"]

    improver = ParallelImprover(
        worktree_path=tmp_path,
        worker_runner=lambda _st, _p, _cwd: (True, "ok"),
        ruff_runner=lambda _files, _cwd: (True, "ok"),
        pytest_runner=lambda tests, _cwd: (pytest_calls.append(list(tests)) or True, "ok"),
        changed_files_inspector=inspector,
    )
    res = improver.run(plan)
    assert res.success is False
    assert res.commit_allowed is False
    assert res.pytest_ran is False
    assert pytest_calls == []
    assert res.out_of_partition_files == ("src/agent_factory/unexpected_other.py",)


def test_staging_manager_apply_parallel_plan_commits_only_on_success_and_preserves_main(
    temp_git_repo: Path,
) -> None:
    initial_main_sha = _git(temp_git_repo, "rev-parse", "main").stdout.strip()
    manager = StagingManager(repo_root=temp_git_repo)
    plan = build_disjoint_plan(
        title="Add parallel calculator module",
        files=[
            "src/agent_factory/calc_helper.py",
            "tests/test_calc_helper.py",
        ],
        contracts=(
            ModuleContract(
                module="agent_factory.calc_helper",
                symbols=("add_pair(a: int, b: int) -> int",),
                description="Adds two integers.",
            ),
        ),
    )

    ruff_calls: list[list[str]] = []
    pytest_calls: list[list[str]] = []

    def simulated_worker(subtask: ParallelSubtask, _prompt: str, wt_path: Path) -> tuple[bool, str]:
        if subtask.role == "logic":
            target = wt_path / "src" / "agent_factory" / "calc_helper.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("def add_pair(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
        elif subtask.role == "tests":
            target = wt_path / "tests" / "test_calc_helper.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "from agent_factory.calc_helper import add_pair\n\n"
                "def test_add_pair() -> None:\n"
                "    assert add_pair(2, 3) == 5\n",
                encoding="utf-8",
            )
        return True, f"written {subtask.assigned_files}"

    try:
        # 1. Failing final targeted pytest must block commit on liwi-staging and clean worktree
        fail_res = manager.apply_parallel_plan(
            plan,
            worker_runner=simulated_worker,
            ruff_runner=lambda files, _cwd: (True, f"ok {files}"),
            pytest_runner=lambda tests, _cwd: (False, f"1 failed in {tests}"),
        )
        assert fail_res.success is False
        assert fail_res.pytest_ran is True
        assert fail_res.pytest_ok is False
        assert fail_res.commit_allowed is False
        assert fail_res.commit_sha is None
        status_fail = manager.get_staging_status()
        assert status_fail["pending_count"] == 0
        assert status_fail["last_error"] is not None
        assert _git(temp_git_repo, "rev-parse", "main").stdout.strip() == initial_main_sha
        assert _git(manager.worktree_path, "status", "--porcelain").stdout.strip() == ""

        # 2. Passing workers + ruff + single targeted pytest commits once on liwi-staging, leaving main untouched
        ok_res = manager.apply_parallel_plan(
            plan,
            worker_runner=simulated_worker,
            ruff_runner=lambda files, _cwd: (ruff_calls.append(list(files)) or True, "ok"),
            pytest_runner=lambda tests, _cwd: (pytest_calls.append(list(tests)) or True, "1 passed"),
        )
        assert ok_res.success is True
        assert ok_res.commit_allowed is True
        assert ok_res.commit_sha is not None and len(ok_res.commit_sha) >= 7
        assert sorted(ruff_calls) == [
            ["src/agent_factory/calc_helper.py"],
            ["tests/test_calc_helper.py"],
        ]
        assert pytest_calls == [["tests/test_calc_helper.py"]]

        assert _git(temp_git_repo, "rev-parse", "main").stdout.strip() == initial_main_sha
        assert not (temp_git_repo / "src" / "agent_factory" / "calc_helper.py").exists()
        assert (manager.worktree_path / "src" / "agent_factory" / "calc_helper.py").is_file()

        status_ok = manager.get_staging_status()
        assert status_ok["pending_count"] == 1
        assert status_ok["last_error"] is None
        assert "Add parallel calculator module" in status_ok["commits"][0]["title"]
    finally:
        manager.shutdown()


def test_staging_manager_enqueue_opt_in_parallel_plan(temp_git_repo: Path) -> None:
    initial_main_sha = _git(temp_git_repo, "rev-parse", "main").stdout.strip()
    manager = StagingManager(repo_root=temp_git_repo)
    plan = build_disjoint_plan(
        title="Queued parallel improvement",
        files=["src/agent_factory/queued_mod.py", "tests/test_queued_mod.py"],
    )

    def simulated_worker(subtask: ParallelSubtask, _prompt: str, wt_path: Path) -> tuple[bool, str]:
        for rel in subtask.assigned_files:
            target = wt_path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# generated\n", encoding="utf-8")
        return True, "ok"

    try:
        manager.enqueue_improvement(
            title="Queued parallel improvement",
            prompt="Apply queued parallel plan",
            metadata={
                "parallel_plan": plan,
                "worker_runner": simulated_worker,
                "ruff_runner": lambda _f, _c: (True, "ok"),
                "pytest_runner": lambda _t, _c: (True, "ok"),
            },
        )
        assert manager.wait_until_idle(timeout=15.0) is True
        status = manager.get_staging_status()
        assert status["pending_count"] == 1
        assert status["last_error"] is None
        assert _git(temp_git_repo, "rev-parse", "main").stdout.strip() == initial_main_sha
    finally:
        manager.shutdown()
