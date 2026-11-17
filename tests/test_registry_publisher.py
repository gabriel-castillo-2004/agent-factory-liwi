"""Tests for GitRegistryPublisher on isolated registry branch and VCS failure handling (AC-3.4, INV-03, INV-11, INV-21, INV-22, INV-23)."""

import json
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_factory.adapters.fake_code_agent import FakeCodeAgent
from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.fake_runner import FakeRunner
from agent_factory.adapters.fake_vcs import FakeVCS
from agent_factory.adapters.git_registry_publisher import GitRegistryPublisher
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.builder import BuildWorker, JobState
from agent_factory.domain.events import EventType
from agent_factory.domain.models import (
    AcceptanceTest,
    AcceptanceTestType,
    ApprovalEvent,
    FileSpec,
    Plan,
    ProjectLinks,
    ProjectStatus,
)
from agent_factory.domain.progress import project_status_from_events
from agent_factory.domain.states import ProjectState
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.publisher import RegistryPublisher, RegistryPublishError
from agent_factory.ports.runner import CommandResult
from agent_factory.ports.vcs import CIRunResult
from agent_factory.workspace import SandboxViolationError, WorkspaceManager


def _init_offline_bare_git_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create a local bare git remote with an initial commit on main (100% offline)."""
    bare_repo = tmp_path / "origin.git"
    seed_repo = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", str(bare_repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "clone", str(bare_repo), str(seed_repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(seed_repo), "checkout", "-b", "main"], check=True, capture_output=True, text=True)
    (seed_repo / "README.md").write_text("# Agent Factory Repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed_repo), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(seed_repo),
            "-c",
            "user.name=Test Seed",
            "-c",
            "user.email=seed@example.local",
            "commit",
            "-m",
            "chore: initial main commit",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "-C", str(seed_repo), "push", "-u", "origin", "main"], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(bare_repo), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    )
    main_sha = subprocess.run(
        ["git", "-C", str(bare_repo), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return bare_repo, main_sha


def test_git_registry_publisher_updates_registry_json_and_project_file_without_touching_main(
    tmp_path: Path,
) -> None:
    """AC-3.4 positive (INV-11, INV-21, INV-23): Publishes registry.json + projects/<id>.json on registry branch cleanly without modifying main."""
    bare_repo, main_sha_before = _init_offline_bare_git_repo(tmp_path)

    publisher = GitRegistryPublisher(
        repo_path=bare_repo,
        branch="registry",
        push_to_remote=True,
        runner=SubprocessRunner(default_timeout_seconds=15.0),
        work_dir=tmp_path / "registry_work",
    )
    assert isinstance(publisher, RegistryPublisher)

    ts = datetime(2026, 11, 16, 22, 15, tzinfo=UTC)
    status_alpha = ProjectStatus(
        project_id="proj-alpha",
        name="Alpha App",
        objective="Static calculator",
        state=ProjectState.DELIVERED,
        phase="DELIVERED",
        progress_pct=100,
        plan_hash="hash-alpha-01",
        links=ProjectLinks(
            repo="https://github.com/org/proj-alpha",
            deploy="https://org.github.io/proj-alpha",
        ),
        updated_at=ts,
    )
    status_beta = ProjectStatus(
        project_id="proj-beta",
        name="Beta App",
        objective="Habit tracker",
        state=ProjectState.DELIVERED,
        phase="DELIVERED",
        progress_pct=100,
        plan_hash="hash-beta-01",
        links=ProjectLinks(
            repo="https://github.com/org/proj-beta",
            deploy="https://org.github.io/proj-beta",
        ),
        updated_at=ts,
    )

    entry_a = publisher.publish_project(status_alpha)
    entry_b = publisher.publish_project(status_beta)
    assert entry_a.project_id == "proj-alpha"
    assert entry_b.deploy_url == "https://org.github.io/proj-beta"

    # Idempotent re-publish updates metadata without duplicating project entries
    updated_alpha = status_alpha.model_copy(
        update={
            "links": ProjectLinks(
                repo="https://github.com/org/proj-alpha",
                deploy="https://org.github.io/proj-alpha-v2",
            )
        }
    )
    publisher.publish_project(updated_alpha)

    # Verify bare main branch SHA was NEVER touched
    main_sha_after = subprocess.run(
        ["git", "-C", str(bare_repo), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert main_sha_after == main_sha_before

    # Verify registry.json and projects/<id>.json directly from git refs/heads/registry
    raw_index = subprocess.run(
        ["git", "-C", str(bare_repo), "show", "refs/heads/registry:registry.json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    index_data = json.loads(raw_index)
    assert sorted(index_data["projects"].keys()) == ["proj-alpha", "proj-beta"]
    assert index_data["projects"]["proj-alpha"]["deploy_url"] == "https://org.github.io/proj-alpha-v2"
    assert index_data["projects"]["proj-beta"]["repo_url"] == "https://github.com/org/proj-beta"
    assert index_data["projects"]["proj-beta"]["state"] == "DELIVERED"

    raw_alpha = subprocess.run(
        ["git", "-C", str(bare_repo), "show", "refs/heads/registry:projects/proj-alpha.json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    alpha_data = json.loads(raw_alpha)
    assert alpha_data["project_id"] == "proj-alpha"
    assert alpha_data["progress_pct"] == 100

    loaded_index = publisher.load_index()
    assert sorted(loaded_index.keys()) == ["proj-alpha", "proj-beta"]
    assert loaded_index["proj-alpha"].deploy_url == "https://org.github.io/proj-alpha-v2"


def test_git_registry_publisher_rejects_main_branch_invalid_id_and_handles_vcs_failure(
    tmp_path: Path,
) -> None:
    """AC-3.4 negative (INV-11, INV-21, INV-22, INV-23): Refuses main/master branch, invalid project_id, and handles git/VCS errors cleanly (PROJECT_FAILED / RegistryPublishError)."""
    bare_repo, main_sha_before = _init_offline_bare_git_repo(tmp_path)

    # 1. Refuses writing registry onto main or master branch
    for bad_branch in ("main", "master"):
        bad_pub = GitRegistryPublisher(
            repo_path=bare_repo,
            branch=bad_branch,
            runner=SubprocessRunner(),
            work_dir=tmp_path / f"bad_wt_{bad_branch}",
        )
        valid_status = ProjectStatus(
            project_id="proj-safe",
            state=ProjectState.DELIVERED,
            progress_pct=100,
        )
        with pytest.raises((ValueError, SandboxViolationError)):
            bad_pub.publish_project(valid_status)

    main_sha_after = subprocess.run(
        ["git", "-C", str(bare_repo), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert main_sha_after == main_sha_before

    # 1b. Refuses invalid/traversal project_id without invoking git runner (INV-22)
    guard_runner = FakeRunner()
    guard_pub = GitRegistryPublisher(
        repo_path=bare_repo,
        branch="registry",
        runner=guard_runner,
        work_dir=tmp_path / "guard_wt",
    )
    evil_status = ProjectStatus.model_construct(
        project_id="../evil",
        state=ProjectState.DELIVERED,
        progress_pct=100,
    )
    with pytest.raises((ValueError, SandboxViolationError)):
        guard_pub.publish_project(evil_status)
    assert len(guard_runner.calls) == 0

    # 2. Handles git push/network failure via FakeRunner with clean RegistryPublishError
    failing_runner = FakeRunner(
        results=[CommandResult(exit_code=128, stderr="fatal: repository unreachable")]
    )
    reg_pub_fail = GitRegistryPublisher(
        repo_path=bare_repo,
        branch="registry",
        runner=failing_runner,
        work_dir=tmp_path / "fail_wt",
    )
    with pytest.raises(RegistryPublishError) as reg_err:
        reg_pub_fail.publish_project(
            ProjectStatus(project_id="proj-net-fail", state=ProjectState.DELIVERED, progress_pct=100)
        )
    assert "unreachable" in str(reg_err.value).lower() or "failed" in str(reg_err.value).lower()

    # 3. VCS delivery failure inside BuildWorker emits PROJECT_FAILED cleanly
    store = MemoryEventStore()
    plan = Plan(
        summary="VCS fail check",
        rationale="Simple",
        template_id="static-web",
        files=[FileSpec(path="src/app.js", purpose="App", contract="export {};", depends_on=[])],
        acceptance_tests=[AcceptanceTest(id="AT-01", description="Works", type=AcceptanceTestType.UNIT)],
        risks=[],
        deployment_target="github-pages",
    )
    llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "VCS fail",
                    "users": ["Gabriel"],
                    "features": ["f1"],
                    "business_rules": ["r1"],
                    "edge_cases": ["e1"],
                    "constraints": ["c1"],
                    "success_criteria": ["s1"],
                    "assumptions": [],
                },
                "questions": [],
            },
            {"approach": "a", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["c"], "robust_proposal": "r", "architectural_safeguards": []},
            plan.model_dump(mode="json"),
        ]
    )
    orch = Orchestrator(store=store, llm=llm)
    orch.create_project("proj-vcs-fail", "VCS Fail", "Check VCS fail handling")
    orch.handle_interview_message("proj-vcs-fail", "ev-1", "Full spec")
    waiting = orch.run_debate("proj-vcs-fail")
    assert waiting.plan_hash is not None
    orch.submit_approval(
        "proj-vcs-fail",
        ApprovalEvent(
            plan_hash=waiting.plan_hash,
            timestamp=datetime(2026, 11, 16, 22, 16, tzinfo=UTC),
            decision="approved",
        ),
    )

    class _FailingMergeVCS(FakeVCS):
        def merge_and_deploy(self, project_id: str, branch: str = "work") -> tuple[str, str]:
            raise RuntimeError("remote rejected branch protection check")

    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws"),
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_app.py": "def test_ok(): assert True\n"}],
            impl_steps=[{"src/app.js": "export const ok = 1;\n"}],
        ),
        runner=FakeRunner(
            results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]
        ),
        vcs=_FailingMergeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
        registry_publisher=reg_pub_fail,
    )
    report = worker.run_job("proj-vcs-fail")
    assert orch.get_status("proj-vcs-fail").state == ProjectState.FAILED
    assert worker.queue.get_job("proj-vcs-fail").state == JobState.FAILED
    assert report.failure_cause is not None
    assert "remote rejected" in report.failure_cause
    assert any(e.type == EventType.PROJECT_FAILED for e in store.get_events("proj-vcs-fail"))


def test_build_worker_registry_failure_preserves_delivered_state_without_illegal_transition(
    tmp_path: Path,
) -> None:
    """A3-01 (INV-04, INV-11, INV-19, ADR-003): Registry publish error after PROJECT_DELIVERED preserves DELIVERED without emitting illegal PROJECT_FAILED."""
    secret_tok = "TEST_REG_WARN_SECRET_5544"
    store = MemoryEventStore()
    plan = Plan(
        summary="Registry fail tolerance",
        rationale="Static",
        template_id="static-web",
        files=[FileSpec(path="src/app.js", purpose="App", contract="export {};", depends_on=[])],
        acceptance_tests=[AcceptanceTest(id="AT-01", description="Pass", type=AcceptanceTestType.UNIT)],
        risks=[],
        deployment_target="github-pages",
    )
    llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "Registry warning check",
                    "users": ["Gabriel"],
                    "features": ["f1"],
                    "business_rules": ["r1"],
                    "edge_cases": ["e1"],
                    "constraints": ["c1"],
                    "success_criteria": ["s1"],
                    "assumptions": [],
                },
                "questions": [],
            },
            {"approach": "a", "components": ["src/app.js"], "tradeoffs": []},
            {"critique": ["c"], "robust_proposal": "r", "architectural_safeguards": []},
            plan.model_dump(mode="json"),
        ]
    )
    orch = Orchestrator(store=store, llm=llm)
    pid = "proj-reg-warn"
    orch.create_project(pid, "Registry Warn", "Verify best-effort registry")
    orch.handle_interview_message(pid, "ev-reg-1", "Full spec")
    waiting = orch.run_debate(pid)
    assert waiting.plan_hash is not None
    orch.submit_approval(
        pid,
        ApprovalEvent(
            plan_hash=waiting.plan_hash,
            timestamp=datetime(2026, 11, 16, 22, 40, tzinfo=UTC),
            decision="approved",
        ),
    )

    failing_reg = GitRegistryPublisher(
        repo_path=tmp_path / "origin.git",
        branch="registry",
        runner=FakeRunner(
            results=[
                CommandResult(
                    exit_code=128,
                    stderr=f"fatal: registry push rejected GITHUB_TOKEN={secret_tok}",
                )
            ]
        ),
        work_dir=tmp_path / "reg_work",
        secrets=(secret_tok,),
    )
    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws"),
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_app.py": "def test_ok(): assert True\n"}],
            impl_steps=[{"src/app.js": "export const ok = 1;\n"}],
        ),
        runner=FakeRunner(
            results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]
        ),
        vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
        registry_publisher=failing_reg,
        secrets_to_redact=(secret_tok,),
    )

    report = worker.run_job(pid)

    # Must NOT poison EventStore with DELIVERED -> FAILED transition
    status = orch.get_status(pid)
    events = store.get_events(pid)
    rebuilt = project_status_from_events(pid, events)
    assert rebuilt == status
    assert status.state == ProjectState.DELIVERED
    assert status.progress_pct == 100
    assert status.links.repo == "https://github.com/agent-factory-test/proj-reg-warn"
    assert not any(e.type == EventType.PROJECT_FAILED for e in events)

    # Failure must still be recorded in sanitized form (kills M16 and M17)
    events_dump = "\n".join(e.model_dump_json() for e in events)
    assert secret_tok not in events_dump
    assert secret_tok not in (report.failure_cause or "")
    delivered_idx = [e.type for e in events].index(EventType.PROJECT_DELIVERED)
    post_delivered = events[delivered_idx + 1 :]
    assert post_delivered, "Expected sanitized registry warning/failure event after delivery"
    assert any(
        "registry" in str(e.type).lower() or "registry" in json.dumps(e.payload).lower()
        for e in post_delivered
    )
    assert report.failure_cause is not None
    assert "registry" in report.failure_cause.lower()
    assert "[REDACTED]" in events_dump or "[REDACTED]" in report.failure_cause


def test_git_registry_publisher_retry_after_push_fail_rejects_refs_main_and_crud(
    tmp_path: Path,
) -> None:
    """A3-06 / A3-09 (INV-21, INV-22, INV-23): Retries git push deterministically, blocks refs/heads/main|master, covers CRUD, corrupt JSON, and isolates main files."""
    bare_repo, main_sha_before = _init_offline_bare_git_repo(tmp_path)
    real_runner = SubprocessRunner(default_timeout_seconds=15.0)
    fixed_ts = datetime(2026, 11, 16, 22, 45, tzinfo=UTC)

    # 1. Rejects refs/heads/main, refs/heads/master, MAIN, and flag-like branch names without invoking git
    for bad_branch in ("main", "master", "refs/heads/main", "refs/heads/master", "  MAIN  ", "-bad"):
        guard_runner = FakeRunner()
        bad_pub = GitRegistryPublisher(
            repo_path=bare_repo,
            branch=bad_branch,
            runner=guard_runner,
            work_dir=tmp_path / "bad_branch_wt",
        )
        with pytest.raises((ValueError, SandboxViolationError)):
            bad_pub.publish_project(
                ProjectStatus(project_id="proj-safe", state=ProjectState.DELIVERED, progress_pct=100)
            )
        assert len(guard_runner.calls) == 0

    # 2. Retry after transient git push failure with identical deterministic updated_at MUST push
    class _FailFirstPushRunner:
        def __init__(self) -> None:
            self.fail_next_push = True

        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            if self.fail_next_push and len(command) >= 2 and command[1] == "push":
                self.fail_next_push = False
                return CommandResult(exit_code=128, stderr="fatal: temporary push blip")
            return real_runner.run(command, cwd, timeout_seconds=timeout_seconds, env=env)

    retry_pub = GitRegistryPublisher(
        repo_path=bare_repo,
        branch="registry",
        push_to_remote=True,
        runner=_FailFirstPushRunner(),
        work_dir=tmp_path / "retry_wt",
    )
    retry_status = ProjectStatus(
        project_id="proj-retry",
        name="Retry App",
        objective="Idempotent push",
        state=ProjectState.DELIVERED,
        progress_pct=100,
        links=ProjectLinks(
            repo="https://github.com/org/proj-retry",
            deploy="https://org.github.io/proj-retry",
        ),
        updated_at=fixed_ts,
    )
    with pytest.raises(RegistryPublishError):
        retry_pub.publish_project(retry_status)

    # Second attempt with identical ProjectStatus and updated_at must push to remote
    retry_pub.publish_project(retry_status)
    raw_after_retry = subprocess.run(
        ["git", "-C", str(bare_repo), "show", "refs/heads/registry:registry.json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "proj-retry" in json.loads(raw_after_retry)["projects"]

    # 3. Orphan branch must not inherit README.md from main (A3-09)
    tree_files = subprocess.run(
        ["git", "-C", str(bare_repo), "ls-tree", "-r", "--name-only", "refs/heads/registry"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "README.md" not in tree_files

    # 4. Separate publisher instance (sync/pull existing remote branch) + publish/load/load_index/corrupt JSON/delete
    pub2 = GitRegistryPublisher(
        repo_path=bare_repo,
        branch="registry",
        push_to_remote=True,
        runner=real_runner,
        work_dir=tmp_path / "pub2_wt",
    )
    status_gamma = ProjectStatus(
        project_id="proj-gamma",
        name="Gamma",
        objective="Test CRUD",
        state=ProjectState.DELIVERED,
        progress_pct=100,
        updated_at=fixed_ts,
    )
    pub2.publish(status_gamma)

    # Retry instance on stale work_dir must sync without non-fast-forward rejection
    retry_pub.publish_project(
        ProjectStatus(
            project_id="proj-delta",
            name="Delta",
            objective="Sync check",
            state=ProjectState.DELIVERED,
            progress_pct=100,
            updated_at=fixed_ts,
        )
    )

    # Fresh reader without work_dir reads from git refs/heads/registry
    reader = GitRegistryPublisher(
        repo_path=bare_repo,
        branch="registry",
        runner=real_runner,
        work_dir=tmp_path / "absent_reader_wt",
    )
    assert sorted(reader.load_index().keys()) == ["proj-delta", "proj-gamma", "proj-retry"]
    loaded_gamma = reader.load("proj-gamma")
    assert loaded_gamma is not None and loaded_gamma.project_id == "proj-gamma"
    assert reader.load("no-such-project") is None

    # Corrupt local registry.json recovers on next publish
    (tmp_path / "pub2_wt" / "registry.json").write_text("{corrupt json!!!", encoding="utf-8")
    pub2.publish_project(status_gamma)
    assert pub2.load("proj-gamma") is not None

    # Delete removes entry and per-project file from remote registry branch
    pub2.delete("proj-gamma")
    raw_after_del = subprocess.run(
        ["git", "-C", str(bare_repo), "show", "refs/heads/registry:registry.json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "proj-gamma" not in json.loads(raw_after_del)["projects"]

    main_sha_after = subprocess.run(
        ["git", "-C", str(bare_repo), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert main_sha_after == main_sha_before
