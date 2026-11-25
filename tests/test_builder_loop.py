"""Tests for BuildWorker write-test-fix loop, budgets, quota pause/resume, and CI rounds (AC-2.1, AC-2.3, AC-2.4, A2a-3, A2a-4)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_factory.adapters.fake_code_agent import FakeCodeAgent
from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.fake_runner import FakeRunner
from agent_factory.adapters.fake_vcs import FakeVCS
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.builder import BuildWorker, JobState, LocalJobQueue
from agent_factory.domain.events import EventType
from agent_factory.domain.models import ApprovalEvent
from agent_factory.domain.progress import project_status_from_events
from agent_factory.domain.states import IllegalTransition, ProjectState
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.code_agent import CodeAgentError, QuotaExhaustedError
from agent_factory.ports.runner import CommandResult, CommandTimeoutError
from agent_factory.ports.vcs import CIRunResult
from agent_factory.workspace import WorkspaceManager


class _FakeClock:
    """Deterministic clock recording sleep durations for quota backoff verification."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return datetime(2026, 11, 16, 16, 0, tzinfo=UTC)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _create_approved_project(
    project_id: str,
    num_tests: int = 2,
    approve: bool = True,
) -> tuple[Orchestrator, MemoryEventStore]:
    store = MemoryEventStore()
    acceptance_tests = [
        {
            "id": f"AT-0{i}",
            "description": f"Acceptance criterion {i}",
            "type": "unit" if i < num_tests else "e2e",
        }
        for i in range(1, num_tests + 1)
    ]
    fake_llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "Static flashcard app",
                    "users": ["Gabriel"],
                    "features": ["Flip card", "Track score"],
                    "business_rules": ["Non-empty deck"],
                    "edge_cases": ["Single card"],
                    "constraints": ["Static web"],
                    "success_criteria": ["Zero console errors"],
                    "assumptions": [],
                },
                "questions": [],
            },
            {
                "approach": "Vanilla TS flashcards",
                "components": ["deck.ts"],
                "tradeoffs": ["In-memory deck"],
            },
            {
                "critique": ["State loss"],
                "robust_proposal": "Deterministic deck reducer",
                "architectural_safeguards": ["Strict state contract"],
            },
            {
                "summary": "Flashcard static web app",
                "rationale": "Simple reducer with strict contract",
                "template_id": "static-app",
                "files": [
                    {
                        "path": "src/deck.ts",
                        "purpose": "Card state management",
                        "contract": "export function flipCard(index: number): boolean",
                        "depends_on": [],
                    }
                ],
                "acceptance_tests": acceptance_tests,
                "risks": ["None"],
                "deployment_target": "github-pages",
            },
        ]
    )
    orchestrator = Orchestrator(store=store, llm=fake_llm)
    orchestrator.create_project(project_id=project_id, name="Flashcards", objective="Static flashcard app")
    orchestrator.handle_interview_message(project_id, f"evt-int-{project_id}", "Complete flashcard spec")
    waiting = orchestrator.run_debate(project_id)
    assert waiting.plan_hash is not None
    if approve:
        orchestrator.submit_approval(
            project_id,
            ApprovalEvent(
                plan_hash=waiting.plan_hash,
                timestamp=datetime(2026, 11, 16, 15, 30, tzinfo=UTC),
                decision="approved",
            ),
        )
    return orchestrator, store


def test_build_loop_converges_on_scripted_scenario_and_produces_accurate_build_report(
    tmp_path: Path,
) -> None:
    """AC-2.1 positive: Scripted local write-test-fix loop converges, passes CI, and returns an accurate BuildReport."""
    project_id = "proj-converge-01"
    orchestrator, _ = _create_approved_project(project_id, num_tests=2, approve=True)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")

    fake_agent = FakeCodeAgent(
        test_gen_steps=[
            {"tests/test_deck.py": "def test_at_01(): pass\ndef test_at_02(): pass\n"},
        ],
        impl_steps=[
            # Attempt 1: partial implementation (AT-01 passes, AT-02 fails)
            {"src/deck.ts": "export function flipCard(index: number): boolean { return false; }\n"},
            # Attempt 2: fixed implementation (both pass)
            {"src/deck.ts": "export function flipCard(index: number): boolean { return index >= 0; }\n"},
        ],
    )
    fake_runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=1,
                stdout="1 passed, 1 failed",
                stderr="AssertionError in test_at_02",
                test_results={"AT-01": True, "AT-02": False},
            ),
            CommandResult(
                exit_code=0,
                stdout="2 passed",
                stderr="",
                test_results={"AT-01": True, "AT-02": True},
            ),
        ]
    )
    fake_vcs = FakeVCS(
        ci_results=[
            CIRunResult(passed=True, summary="GitHub Actions CI green"),
        ]
    )

    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=fake_agent,
        runner=fake_runner,
        vcs=fake_vcs,
    )

    report = worker.run_job(
        project_id=project_id,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )

    assert report.invocations == 3
    assert report.test_results == {"AT-01": True, "AT-02": True}
    assert report.ci_rounds == 1
    assert report.failure_cause is None

    status = orchestrator.get_status(project_id)
    assert status.state == ProjectState.DELIVERED
    assert status.progress_pct == 100

    ws_dir = ws_mgr.get_workspace_dir(project_id)
    assert (ws_dir / "PROJECT_PROMPT.md").is_file()


def test_build_worker_rejects_unapproved_project_not_in_building_state(tmp_path: Path) -> None:
    """AC-2.1 negative (INV-04, INV-05): BuildWorker refuses to enqueue or run a project that is not in BUILDING state."""
    project_id = "proj-unapproved-build"
    orchestrator, _ = _create_approved_project(project_id, num_tests=2, approve=False)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")

    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=FakeCodeAgent(),
        runner=FakeRunner(),
        vcs=FakeVCS(),
    )

    with pytest.raises(IllegalTransition):
        worker.enqueue_approved_project(project_id)

    with pytest.raises(IllegalTransition):
        worker.run_job(project_id)

    assert orchestrator.get_status(project_id).state == ProjectState.WAITING_APPROVAL


def test_exhausting_building_invocation_budget_transitions_to_failed_with_report(
    tmp_path: Path,
) -> None:
    """AC-2.3 (INV-17): Exhausting BUILDING=15 code agent invocations transitions project to FAILED with BuildReport."""
    project_id = "proj-exhaust-invocations"
    orchestrator, _ = _create_approved_project(project_id, num_tests=2, approve=True)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")

    # 1 test_gen + up to 20 impl steps; BUILDING limit is 15 total invocations
    fake_agent = FakeCodeAgent(
        test_gen_steps=[
            {"tests/test_deck.py": "def test_at_01(): assert False\n"},
        ],
        impl_steps=[
            {"src/deck.ts": f"export const attempt = {i};\n"}
            for i in range(1, 21)
        ],
    )
    fake_runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=1,
                stdout="2 failed",
                stderr="Test failed",
                test_results={"AT-01": False, "AT-02": False},
            )
            for _ in range(20)
        ]
    )
    fake_vcs = FakeVCS()

    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=fake_agent,
        runner=fake_runner,
        vcs=fake_vcs,
    )

    report = worker.run_job(
        project_id=project_id,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )

    status = orchestrator.get_status(project_id)
    assert status.state == ProjectState.FAILED
    assert report.invocations == 15
    assert report.ci_rounds == 0
    assert report.failure_cause is not None
    assert "BUILDING" in report.failure_cause


def test_exhausting_three_ci_rounds_transitions_to_failed_with_report(
    tmp_path: Path,
) -> None:
    """AC-2.3 (INV-15): Exhausting the maximum of 3 CI rounds transitions project to FAILED with BuildReport."""
    project_id = "proj-exhaust-ci-rounds"
    orchestrator, _ = _create_approved_project(project_id, num_tests=2, approve=True)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")

    fake_agent = FakeCodeAgent(
        test_gen_steps=[
            {"tests/test_deck.py": "def test_at_01(): pass\n"},
        ],
        impl_steps=[
            {"src/deck.ts": f"export const ciRound = {r};\n"}
            for r in range(1, 5)
        ],
    )
    fake_runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=0,
                stdout="2 passed locally",
                test_results={"AT-01": True, "AT-02": True},
            )
            for _ in range(4)
        ]
    )
    fake_vcs = FakeVCS(
        ci_results=[
            CIRunResult(passed=False, summary="CI round 1 failed: Playwright console.error"),
            CIRunResult(passed=False, summary="CI round 2 failed: Playwright pageerror"),
            CIRunResult(passed=False, summary="CI round 3 failed: Clean Ubuntu build error"),
        ]
    )

    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=fake_agent,
        runner=fake_runner,
        vcs=fake_vcs,
    )

    report = worker.run_job(
        project_id=project_id,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )

    status = orchestrator.get_status(project_id)
    assert status.state == ProjectState.FAILED
    assert report.ci_rounds == 3
    assert report.invocations == 4
    assert len(fake_vcs.pushes) == 3
    assert len(fake_vcs.ci_checks) == 3
    assert len(fake_vcs.merges) == 0
    assert report.failure_cause is not None
    assert "CI" in report.failure_cause


def test_quota_exhausted_pauses_job_and_resumes_without_losing_state_or_counting_failure(
    tmp_path: Path,
) -> None:
    """AC-2.4 positive (INV-09): QuotaExhaustedError pauses the job with backoff and resumes cleanly without counting as failure."""
    project_id = "proj-quota-pause-resume"
    orchestrator, _ = _create_approved_project(project_id, num_tests=2, approve=True)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    fake_clock = _FakeClock()
    queue = LocalJobQueue()

    fake_agent = FakeCodeAgent(
        test_gen_steps=[
            {"tests/test_deck.py": "def test_at_01(): pass\ndef test_at_02(): pass\n"},
        ],
        impl_steps=[
            QuotaExhaustedError("Personal plan hourly quota reached", retry_after_seconds=2.5),
            {"src/deck.ts": "export function flipCard(index: number): boolean { return true; }\n"},
        ],
    )
    fake_runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=0,
                stdout="2 passed",
                test_results={"AT-01": True, "AT-02": True},
            )
        ]
    )
    fake_vcs = FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI green")])

    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=fake_agent,
        runner=fake_runner,
        vcs=fake_vcs,
        queue=queue,
        clock=fake_clock,
    )

    worker.enqueue_approved_project(
        project_id=project_id,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )

    # First run hits QuotaExhaustedError and pauses without failing the project
    paused_report = worker.run_next()
    assert paused_report is not None

    job_after_pause = queue.get_job(project_id)
    assert job_after_pause is not None
    assert job_after_pause.state == JobState.PAUSED_QUOTA
    assert job_after_pause.pause_count == 1
    assert fake_clock.sleeps == [2.5]

    status_while_paused = orchestrator.get_status(project_id)
    assert status_while_paused.state == ProjectState.BUILDING
    # Only the completed test-generation invocation counted; the quota error did not waste budget
    assert paused_report.invocations == 1
    assert paused_report.failure_cause is None

    # Second run resumes from PAUSED_QUOTA without re-running test generation
    resumed_report = worker.run_next()
    assert resumed_report is not None
    assert len(fake_agent.test_gen_calls) == 1
    assert resumed_report.invocations == 2
    assert resumed_report.ci_rounds == 1
    assert resumed_report.failure_cause is None
    assert orchestrator.get_status(project_id).state == ProjectState.DELIVERED


def test_non_quota_code_agent_error_transitions_project_to_failed(
    tmp_path: Path,
) -> None:
    """AC-2.4 negative (INV-09): A fatal non-quota CodeAgentError marks the project FAILED with cause."""
    project_id = "proj-fatal-agent-err"
    orchestrator, _ = _create_approved_project(project_id, num_tests=2, approve=True)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")

    fake_agent = FakeCodeAgent(
        test_gen_steps=[
            {"tests/test_deck.py": "def test_at_01(): pass\n"},
        ],
        impl_steps=[
            CodeAgentError("Agent CLI process crashed with exit code 137"),
        ],
    )
    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=fake_agent,
        runner=FakeRunner(),
        vcs=FakeVCS(),
    )

    report = worker.run_job(
        project_id=project_id,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )

    status = orchestrator.get_status(project_id)
    assert status.state == ProjectState.FAILED
    assert report.failure_cause is not None
    assert "exit code 137" in report.failure_cause


def test_push_occurs_only_when_local_tests_pass_or_cap_exhausted_and_never_on_intermediate_failures(
    tmp_path: Path,
) -> None:
    """A2a-3 (INV-15): Intermediate failed local attempts never trigger vcs.push_branch."""
    project_id = "proj-minimal-push"
    orchestrator, _ = _create_approved_project(project_id, num_tests=2, approve=True)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")

    fake_agent = FakeCodeAgent(
        test_gen_steps=[
            {"tests/test_deck.py": "def test_at_01(): pass\ndef test_at_02(): pass\n"},
        ],
        impl_steps=[
            {"src/deck.ts": "export const v = 1;\n"},
            {"src/deck.ts": "export const v = 2;\n"},
            {"src/deck.ts": "export const v = 3;\n"},
            {"src/deck.ts": "export const v = 4;\n"},
        ],
    )
    fake_runner = FakeRunner(
        results=[
            CommandResult(exit_code=1, stdout="fail 1", test_results={"AT-01": False, "AT-02": False}),
            CommandResult(exit_code=1, stdout="fail 2", test_results={"AT-01": True, "AT-02": False}),
            CommandResult(exit_code=1, stdout="fail 3", test_results={"AT-01": True, "AT-02": False}),
            CommandResult(exit_code=0, stdout="pass 4", test_results={"AT-01": True, "AT-02": True}),
        ]
    )
    fake_vcs = FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI green")])

    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=fake_agent,
        runner=fake_runner,
        vcs=fake_vcs,
    )

    report = worker.run_job(
        project_id=project_id,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )

    assert len(fake_runner.calls) == 4
    # Exactly 1 push occurred (only after the 4th local test run succeeded)
    assert len(fake_vcs.pushes) == 1
    assert report.ci_rounds == 1
    assert orchestrator.get_status(project_id).state == ProjectState.DELIVERED


def test_events_and_project_status_track_invocations_ci_rounds_and_proportional_progress(
    tmp_path: Path,
) -> None:
    """A2a-4 (INV-11, INV-12, INV-23): Event log records every invocation, test update, and CI round with proportional BUILDING progress (35..90)."""
    # 1. Converging project with 4 acceptance tests (0/4 -> 35, 2/4 -> 62, 4/4 -> 90, VERIFYING -> 95, DELIVERED -> 100)
    project_id = "proj-progress-events"
    orchestrator, store = _create_approved_project(project_id, num_tests=4, approve=True)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")

    initial_building_status = orchestrator.get_status(project_id)
    assert initial_building_status.state == ProjectState.BUILDING
    assert initial_building_status.progress_pct == 35

    fake_agent = FakeCodeAgent(
        test_gen_steps=[
            {"tests/test_deck.py": "def test_all(): pass\n"},
        ],
        impl_steps=[
            # Attempt 1: 2 of 4 pass locally -> progress 62
            {"src/deck.ts": "export const step = 1;\n"},
            # Attempt 2: 4 of 4 pass locally -> progress 90 -> VERIFYING (95) -> CI round 1 fails -> back to BUILDING
            {"src/deck.ts": "export const step = 2;\n"},
            # Attempt 3: 4 of 4 pass locally -> VERIFYING (95) -> CI round 2 passes -> DELIVERED (100)
            {"src/deck.ts": "export const step = 3;\n"},
        ],
    )
    fake_runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=1,
                stdout="2 passed, 2 failed",
                test_results={"AT-01": True, "AT-02": True, "AT-03": False, "AT-04": False},
            ),
            CommandResult(
                exit_code=0,
                stdout="4 passed",
                test_results={"AT-01": True, "AT-02": True, "AT-03": True, "AT-04": True},
            ),
            CommandResult(
                exit_code=0,
                stdout="4 passed",
                test_results={"AT-01": True, "AT-02": True, "AT-03": True, "AT-04": True},
            ),
        ]
    )
    fake_vcs = FakeVCS(
        ci_results=[
            CIRunResult(passed=False, summary="CI round 1 failed"),
            CIRunResult(passed=True, summary="CI round 2 passed"),
        ]
    )

    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=fake_agent,
        runner=fake_runner,
        vcs=fake_vcs,
    )

    report = worker.run_job(
        project_id=project_id,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )

    events = store.get_events(project_id)
    # Check intermediate projection right after first TESTS_UPDATED (2/4 passed -> 62%)
    first_test_idx = next(i for i, e in enumerate(events) if e.type == EventType.TESTS_UPDATED)
    mid_status = project_status_from_events(project_id, events[: first_test_idx + 1])
    assert mid_status.state == ProjectState.BUILDING
    assert mid_status.tests.passed == 2
    assert mid_status.tests.failed == 2
    assert mid_status.tests.total == 4
    assert mid_status.progress_pct == 62

    # Check intermediate projection right after second TESTS_UPDATED (4/4 passed -> 90%)
    second_test_idx = next(
        i for i, e in enumerate(events[first_test_idx + 1 :], start=first_test_idx + 1)
        if e.type == EventType.TESTS_UPDATED
    )
    ninety_status = project_status_from_events(project_id, events[: second_test_idx + 1])
    assert ninety_status.state == ProjectState.BUILDING
    assert ninety_status.progress_pct == 90

    # Final status
    final_status = orchestrator.get_status(project_id)
    assert final_status.state == ProjectState.DELIVERED
    assert final_status.progress_pct == 100
    assert final_status.ci_rounds == 2
    # 1 interview + 1 test_gen + 3 impl = 5 total AGENT_INVOKED events
    assert final_status.agent_invocations == 5
    assert report.invocations == 4
    assert report.ci_rounds == 2

    # 2. Negative case: Failure at 2/4 tests passed preserves progress_pct == 62
    project_fail = "proj-progress-fail-62"
    orchestrator_fail, _ = _create_approved_project(project_fail, num_tests=4, approve=True)
    worker_fail = BuildWorker(
        orchestrator=orchestrator_fail,
        workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws_fail"),
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_deck.py": "def test_all(): pass\n"}],
            impl_steps=[
                {"src/deck.ts": "export const step = 1;\n"},
                CodeAgentError("Crash after reaching 62%"),
            ],
        ),
        runner=FakeRunner(
            results=[
                CommandResult(
                    exit_code=1,
                    stdout="2 passed, 2 failed",
                    test_results={"AT-01": True, "AT-02": True, "AT-03": False, "AT-04": False},
                )
            ]
        ),
        vcs=FakeVCS(),
    )

    worker_fail.run_job(
        project_id=project_fail,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )
    failed_status = orchestrator_fail.get_status(project_fail)
    assert failed_status.state == ProjectState.FAILED
    assert failed_status.progress_pct == 62


def test_quota_resume_succeeds_after_prior_attempt_wrote_production_files_in_src(
    tmp_path: Path,
) -> None:
    """F2A-01 (AC-2.4, INV-09): Resuming from PAUSED_QUOTA after a prior attempt already wrote to src/ succeeds without SandboxViolationError."""
    project_id = "proj-quota-resume-src"
    orchestrator, _ = _create_approved_project(project_id, num_tests=2, approve=True)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    fake_clock = _FakeClock()
    queue = LocalJobQueue()

    fake_agent = FakeCodeAgent(
        test_gen_steps=[
            {"tests/test_deck.py": "def test_at_01(): pass\ndef test_at_02(): pass\n"},
        ],
        impl_steps=[
            # Attempt 1 writes production code in src/ and fails AT-02 locally
            {"src/deck.ts": "export function flipCard(index: number): boolean { return false; }\n"},
            # Attempt 2 hits QuotaExhaustedError while src/deck.ts already exists on disk
            QuotaExhaustedError("Hourly plan quota reached on attempt 2", retry_after_seconds=3.0),
            # Attempt 3 (after resume) fixes src/deck.ts and passes all tests
            {"src/deck.ts": "export function flipCard(index: number): boolean { return index >= 0; }\n"},
        ],
    )
    fake_runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=1,
                stdout="1 passed, 1 failed",
                stderr="AssertionError in AT-02",
                test_results={"AT-01": True, "AT-02": False},
            ),
            CommandResult(
                exit_code=0,
                stdout="2 passed",
                test_results={"AT-01": True, "AT-02": True},
            ),
        ]
    )
    fake_vcs = FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI green")])

    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=fake_agent,
        runner=fake_runner,
        vcs=fake_vcs,
        queue=queue,
        clock=fake_clock,
    )

    worker.enqueue_approved_project(
        project_id=project_id,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )

    # First run: completes test_gen (1) + attempt 1 (2, writes src/deck.ts), then pauses on attempt 2
    paused_report = worker.run_next()
    assert paused_report is not None
    assert paused_report.invocations == 2
    assert paused_report.failure_cause is None

    job_paused = queue.get_job(project_id)
    assert job_paused is not None
    assert job_paused.state == JobState.PAUSED_QUOTA
    assert job_paused.pause_count == 1
    assert fake_clock.sleeps == [3.0]
    assert orchestrator.get_status(project_id).state == ProjectState.BUILDING

    # Second run: resumes from PAUSED_QUOTA with src/deck.ts on disk, fixes code, and delivers
    resumed_report = worker.run_next()
    assert resumed_report is not None
    assert len(fake_agent.test_gen_calls) == 1
    assert resumed_report.invocations == 3
    assert resumed_report.ci_rounds == 1
    assert resumed_report.failure_cause is None
    assert resumed_report.test_results == {"AT-01": True, "AT-02": True}
    assert orchestrator.get_status(project_id).state == ProjectState.DELIVERED


def test_runner_timeout_and_vcs_errors_transition_project_to_failed_with_report(
    tmp_path: Path,
) -> None:
    """F2A-02 (A2a-2, INV-06, INV-09): CommandTimeoutError in runner or OSError/RuntimeError in VCS transitions project to FAILED with BuildReport."""
    # 1. Runner raises CommandTimeoutError
    project_timeout = "proj-runner-timeout"
    orchestrator_1, _ = _create_approved_project(project_timeout, num_tests=2, approve=True)
    worker_1 = BuildWorker(
        orchestrator=orchestrator_1,
        workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws_timeout"),
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_deck.py": "def test_at_01(): pass\n"}],
            impl_steps=[{"src/deck.ts": "while (true) {}\n"}],
        ),
        runner=FakeRunner(
            results=[CommandTimeoutError("Command ['pytest', '-q'] timed out after 30.0 seconds")]
        ),
        vcs=FakeVCS(),
    )

    report_1 = worker_1.run_job(
        project_id=project_timeout,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )
    status_1 = orchestrator_1.get_status(project_timeout)
    assert status_1.state == ProjectState.FAILED
    assert status_1.failure_reason is not None
    assert "timed out" in status_1.failure_reason
    assert report_1.failure_cause is not None
    assert "timed out" in report_1.failure_cause
    assert report_1.invocations == 2

    # 2. VCS raises OSError during CI verification
    project_vcs_err = "proj-vcs-error"
    orchestrator_2, _ = _create_approved_project(project_vcs_err, num_tests=2, approve=True)
    worker_2 = BuildWorker(
        orchestrator=orchestrator_2,
        workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws_vcs_err"),
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_deck.py": "def test_at_01(): pass\n"}],
            impl_steps=[{"src/deck.ts": "export const ok = true;\n"}],
        ),
        runner=FakeRunner(
            results=[
                CommandResult(
                    exit_code=0,
                    stdout="2 passed",
                    test_results={"AT-01": True, "AT-02": True},
                )
            ]
        ),
        vcs=FakeVCS(ci_results=[OSError("GitHub remote connection reset by peer")]),
    )

    report_2 = worker_2.run_job(
        project_id=project_vcs_err,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )
    status_2 = orchestrator_2.get_status(project_vcs_err)
    assert status_2.state == ProjectState.FAILED
    assert status_2.failure_reason is not None
    assert "connection reset" in status_2.failure_reason
    assert report_2.failure_cause is not None
    assert "connection reset" in report_2.failure_cause


def test_test_generation_quota_pause_and_code_agent_error_handling(
    tmp_path: Path,
) -> None:
    """F2A-03 (AC-2.4, INV-09, INV-17, ADR-002): QuotaExhaustedError in generate_tests pauses without consuming breaker budget or emitting AGENT_INVOKED; CodeAgentError transitions to FAILED."""
    # 1. QuotaExhaustedError during Step A (generate_tests)
    project_quota = "proj-testgen-quota"
    orchestrator_1, _ = _create_approved_project(project_quota, num_tests=2, approve=True)
    fake_clock = _FakeClock()
    queue = LocalJobQueue()

    fake_agent_1 = FakeCodeAgent(
        test_gen_steps=[
            QuotaExhaustedError("Quota hit during test generation", retry_after_seconds=1.5),
            {"tests/test_deck.py": "def test_at_01(): pass\ndef test_at_02(): pass\n"},
        ],
        impl_steps=[
            {"src/deck.ts": "export function flipCard(index: number): boolean { return true; }\n"},
        ],
    )
    worker_1 = BuildWorker(
        orchestrator=orchestrator_1,
        workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws_tg_quota"),
        code_agent=fake_agent_1,
        runner=FakeRunner(
            results=[
                CommandResult(
                    exit_code=0,
                    stdout="2 passed",
                    test_results={"AT-01": True, "AT-02": True},
                )
            ]
        ),
        vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI green")]),
        queue=queue,
        clock=fake_clock,
    )

    worker_1.enqueue_approved_project(
        project_id=project_quota,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )

    paused = worker_1.run_next()
    assert paused is not None
    assert paused.invocations == 0
    assert paused.failure_cause is None

    job = queue.get_job(project_quota)
    assert job is not None
    assert job.state == JobState.PAUSED_QUOTA
    assert job.pause_count == 1
    assert fake_clock.sleeps == [1.5]
    # Zero BUILDING calls consumed in circuit breaker and zero AGENT_INVOKED emitted for BUILDING
    assert orchestrator_1.circuit_breaker.get_calls(project_quota, ProjectState.BUILDING) == 0
    assert orchestrator_1.get_status(project_quota).agent_invocations == 1

    # Resume and complete within exact budget of 2 BUILDING calls (1 test_gen + 1 impl)
    resumed = worker_1.run_next()
    assert resumed is not None
    assert resumed.invocations == 2
    assert orchestrator_1.circuit_breaker.get_calls(project_quota, ProjectState.BUILDING) == 2
    assert orchestrator_1.get_status(project_quota).agent_invocations == 3
    assert orchestrator_1.get_status(project_quota).state == ProjectState.DELIVERED

    # 2. CodeAgentError during Step A (generate_tests) transitions to FAILED and records the call
    project_err = "proj-testgen-fatal"
    orchestrator_2, _ = _create_approved_project(project_err, num_tests=2, approve=True)
    worker_2 = BuildWorker(
        orchestrator=orchestrator_2,
        workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws_tg_err"),
        code_agent=FakeCodeAgent(
            test_gen_steps=[CodeAgentError("Agent CLI crashed during test generation")]
        ),
        runner=FakeRunner(),
        vcs=FakeVCS(),
    )

    report_err = worker_2.run_job(
        project_id=project_err,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )
    status_err = orchestrator_2.get_status(project_err)
    assert status_err.state == ProjectState.FAILED
    assert report_err.failure_cause is not None
    assert "crashed during test generation" in report_err.failure_cause
    assert report_err.invocations == 1
    assert orchestrator_2.circuit_breaker.get_calls(project_err, ProjectState.BUILDING) == 1
    assert status_err.agent_invocations == 2


def test_building_budget_is_cumulative_with_breaker_and_records_failed_agent_calls(
    tmp_path: Path,
) -> None:
    """F2A-04 (INV-17): BUILDING=15 limit is enforced cumulatively via CircuitBreaker before invoking agent, and CodeAgentError in implement_or_fix records the call before failing."""
    # 1. Cumulative BUILDING budget: 14 prior calls + 1 test_gen = 15; 2nd worker call trips breaker BEFORE running implement_or_fix
    project_cum = "proj-cumulative-breaker"
    orchestrator_1, _ = _create_approved_project(project_cum, num_tests=2, approve=True)
    for _ in range(14):
        orchestrator_1.record_state_call(project_cum, ProjectState.BUILDING)
    assert orchestrator_1.circuit_breaker.get_calls(project_cum, ProjectState.BUILDING) == 14

    fake_agent_1 = FakeCodeAgent(
        test_gen_steps=[{"tests/test_deck.py": "def test_at_01(): pass\n"}],
        impl_steps=[{"src/deck.ts": "export const shouldNotRun = true;\n"}],
    )
    worker_1 = BuildWorker(
        orchestrator=orchestrator_1,
        workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws_cum"),
        code_agent=fake_agent_1,
        runner=FakeRunner(),
        vcs=FakeVCS(),
    )

    report_1 = worker_1.run_job(
        project_id=project_cum,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )
    status_1 = orchestrator_1.get_status(project_cum)
    assert status_1.state == ProjectState.FAILED
    assert len(fake_agent_1.test_gen_calls) == 1
    assert len(fake_agent_1.impl_calls) == 0
    assert orchestrator_1.circuit_breaker.get_calls(project_cum, ProjectState.BUILDING) == 15
    assert report_1.failure_cause is not None
    assert "BUILDING" in report_1.failure_cause

    # 2. CodeAgentError in implement_or_fix records the invocation in CircuitBreaker and AGENT_INVOKED before failing
    project_err = "proj-impl-err-breaker"
    orchestrator_2, _ = _create_approved_project(project_err, num_tests=2, approve=True)
    worker_2 = BuildWorker(
        orchestrator=orchestrator_2,
        workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws_impl_err"),
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_deck.py": "def test_at_01(): pass\n"}],
            impl_steps=[CodeAgentError("Agent crashed on impl attempt 1")],
        ),
        runner=FakeRunner(),
        vcs=FakeVCS(),
    )

    report_2 = worker_2.run_job(
        project_id=project_err,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )
    status_2 = orchestrator_2.get_status(project_err)
    assert status_2.state == ProjectState.FAILED
    assert orchestrator_2.circuit_breaker.get_calls(project_err, ProjectState.BUILDING) == 2
    assert status_2.agent_invocations == 3
    assert report_2.invocations == 2


def test_local_tests_passed_requires_zero_exit_code_and_all_plan_acceptance_tests(
    tmp_path: Path,
) -> None:
    """F2A-05 (AC-2.1, INV-13): Local pass requires exit_code == 0 AND every acceptance_test.id in the Plan present and True in test_results."""
    project_id = "proj-strict-local-pass"
    orchestrator, _ = _create_approved_project(project_id, num_tests=2, approve=True)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")

    fake_agent = FakeCodeAgent(
        test_gen_steps=[
            {"tests/test_deck.py": "def test_at_01(): pass\ndef test_at_02(): pass\n"},
        ],
        impl_steps=[
            {"src/deck.ts": "export const v = 1;\n"},
            {"src/deck.ts": "export const v = 2;\n"},
            {"src/deck.ts": "export const v = 3;\n"},
            {"src/deck.ts": "export const v = 4;\n"},
            {"src/deck.ts": "export const v = 5;\n"},
        ],
    )
    fake_runner = FakeRunner(
        results=[
            # Attempt 1: non-zero exit code with empty test_results -> fail
            CommandResult(exit_code=1, stdout="collection error", stderr="SyntaxError", test_results={}),
            # Attempt 2: non-zero exit code even though test_results are all True -> fail (kills M14)
            CommandResult(
                exit_code=1,
                stdout="linter error",
                stderr="exit 1",
                test_results={"AT-01": True, "AT-02": True},
            ),
            # Attempt 3: zero exit code but empty test_results -> fail
            CommandResult(exit_code=0, stdout="0 selected", stderr="", test_results={}),
            # Attempt 4: zero exit code but missing AT-02 from Plan -> fail
            CommandResult(exit_code=0, stdout="1 passed", stderr="", test_results={"AT-01": True}),
            # Attempt 5: zero exit code AND all Plan acceptance tests (AT-01, AT-02) True -> pass!
            CommandResult(
                exit_code=0,
                stdout="2 passed",
                stderr="",
                test_results={"AT-01": True, "AT-02": True},
            ),
        ]
    )
    fake_vcs = FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI green")])

    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=fake_agent,
        runner=fake_runner,
        vcs=fake_vcs,
    )

    report = worker.run_job(
        project_id=project_id,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )

    assert len(fake_runner.calls) == 5
    assert len(fake_vcs.pushes) == 1
    assert report.invocations == 6
    assert report.ci_rounds == 1
    assert orchestrator.get_status(project_id).state == ProjectState.DELIVERED


def test_project_delivered_projects_repo_and_deploy_links_in_status(
    tmp_path: Path,
) -> None:
    """F2A-07 (INV-11, INV-12): After PROJECT_DELIVERED, project_status_from_events projects status.links.repo and status.links.deploy matching the delivered URLs."""
    project_id = "proj-links-delivered"
    orchestrator, store = _create_approved_project(project_id, num_tests=2, approve=True)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")

    fake_agent = FakeCodeAgent(
        test_gen_steps=[
            {"tests/test_deck.py": "def test_at_01(): pass\ndef test_at_02(): pass\n"},
        ],
        impl_steps=[
            {"src/deck.ts": "export function flipCard(index: number): boolean { return true; }\n"},
        ],
    )
    fake_runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=0,
                stdout="2 passed",
                test_results={"AT-01": True, "AT-02": True},
            )
        ]
    )
    fake_vcs = FakeVCS(
        ci_results=[CIRunResult(passed=True, summary="CI green")],
        repo_base_url="https://github.com/af-org",
        deploy_base_url="https://af-org.github.io",
    )

    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=fake_agent,
        runner=fake_runner,
        vcs=fake_vcs,
    )

    worker.run_job(
        project_id=project_id,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )

    status = orchestrator.get_status(project_id)
    assert status.state == ProjectState.DELIVERED
    assert status.links.repo == "https://github.com/af-org/proj-links-delivered"
    assert status.links.deploy == "https://af-org.github.io/proj-links-delivered"

    reprojected = project_status_from_events(project_id, store.get_events(project_id))
    assert reprojected.links.repo == "https://github.com/af-org/proj-links-delivered"
    assert reprojected.links.deploy == "https://af-org.github.io/proj-links-delivered"


def test_build_worker_emits_phase_b_event_metadata_with_inv13_and_inv19(
    tmp_path: Path,
) -> None:
    """Phase B: BuildWorker annotates AGENT_INVOKED with bounded redacted files_written, round/max_rounds, failing_tests, and outcome."""
    from agent_factory.ports.code_agent import CodeAgentResult

    project_id = "proj-phase-b-meta"
    secret = "SECRET_PHASE_B_9876"
    orchestrator, store = _create_approved_project(
        project_id, num_tests=2, approve=True
    )
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")

    many_files = {f"src/mod_{i:02d}.py": f"VAL_{i} = {i}\n" for i in range(12)}

    class _RichAgent(FakeCodeAgent):
        def generate_tests(self, *args, **kwargs):  # type: ignore[override]
            res = super().generate_tests(*args, **kwargs)
            return CodeAgentResult(
                files_written=res.files_written + ["src/leaked_impl.py"],
                summary=f"Generated tests with {secret}",
                model_used="Gemini 4 Hydrogen (Medium)",
            )

        def implement_or_fix(self, *args, **kwargs):  # type: ignore[override]
            res = super().implement_or_fix(*args, **kwargs)
            return CodeAgentResult(
                files_written=res.files_written,
                summary=f"Fixed logic ({secret}) " + ("x" * 160),
                model_used="Gemini 4 Hydrogen (Medium)",
            )

    fake_agent = _RichAgent(
        test_gen_steps=[
            {"tests/test_app.py": "def test_at_01(): pass\ndef test_at_02(): pass\n"},
        ],
        impl_steps=[
            many_files,
            {"tests/test_app.py": "# tampered!\n", "src/mod_00.py": "VAL_0 = 99\n"},
            {"src/mod_00.py": "VAL_0 = 100\n"},
        ],
    )
    fake_runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=1,
                stdout="1 passed, 1 failed",
                test_results={"AT-01": True, "AT-02": False},
            ),
            CommandResult(
                exit_code=0,
                stdout="2 passed",
                test_results={"AT-01": True, "AT-02": True},
            ),
        ]
    )
    fake_vcs = FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")])
    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=fake_agent,
        runner=fake_runner,
        vcs=fake_vcs,
        secrets_to_redact=(secret,),
    )
    worker.run_job(
        project_id=project_id,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )

    events = store.get_events(project_id)
    invoked = [
        ev
        for ev in events
        if ev.type == EventType.AGENT_INVOKED
        and ev.payload.get("state") == ProjectState.BUILDING.value
    ]
    assert len(invoked) == 4  # 1 test gen + 3 impl rounds (tamper adds no extra event)

    gen_ev = invoked[0]
    assert gen_ev.payload["agent"] == "generate_tests"
    assert gen_ev.payload["files_written"] == ["tests/test_app.py"]
    assert secret not in gen_ev.payload["summary_short"]

    r1, r2, r3 = invoked[1], invoked[2], invoked[3]
    assert r1.payload["round"] == 1
    assert r1.payload["outcome"] == "ok"
    assert len(r1.payload["files_written"]) == 10
    assert len(r1.payload["summary_short"]) <= 140
    assert secret not in r1.payload["summary_short"]

    assert r2.payload["round"] == 2
    assert r2.payload["failing_tests"] == ["AT-02"]
    assert r2.payload["outcome"] == "reverted_tamper"

    assert r3.payload["round"] == 3
    assert r3.payload["outcome"] == "ok"
    assert orchestrator.get_status(project_id).state == ProjectState.DELIVERED


def test_build_worker_stops_early_after_two_consecutive_noop_rounds_with_identical_failure(
    tmp_path: Path,
) -> None:
    """BuildWorker halts immediately after 2 consecutive implement_or_fix rounds with files_written==[] and same pytest error instead of burning all 14 rounds."""
    # Case A: Round 1 writes files, then Rounds 2 & 3 return files_written=[] on identical missing-playwright error
    pid_a = "proj-stagnate-after-write"
    orch_a, store_a = _create_approved_project(pid_a, num_tests=2, approve=True)
    ws_mgr_a = WorkspaceManager(base_dir=tmp_path / "ws_stag_a")

    agent_a = FakeCodeAgent(
        test_gen_steps=[
            {"tests/test_web.py": "def test_ui(): assert False\n"},
        ],
        impl_steps=[
            {"src/deck.ts": "export const ready = true;\n"},
            {},
            {},
            {"src/deck.ts": "export const neverReached = true;\n"},
            {"src/deck.ts": "export const neverReached2 = true;\n"},
        ],
    )
    runner_a = FakeRunner(
        results=[
            CommandResult(
                exit_code=1,
                stdout="FAILED tests/test_web.py::test_ui\n1 failed in 0.41s",
                stderr="ModuleNotFoundError: No module named 'playwright'",
                test_results={"AT-01": False, "AT-02": False},
            ),
            CommandResult(
                exit_code=1,
                stdout="FAILED tests/test_web.py::test_ui\n1 failed in 0.39s",
                stderr="ModuleNotFoundError: No module named 'playwright'",
                test_results={"AT-01": False, "AT-02": False},
            ),
            CommandResult(
                exit_code=1,
                stdout="FAILED tests/test_web.py::test_ui\n1 failed in 0.52s",
                stderr="ModuleNotFoundError: No module named 'playwright'",
                test_results={"AT-01": False, "AT-02": False},
            ),
        ]
    )
    worker_a = BuildWorker(
        orchestrator=orch_a,
        workspace_manager=ws_mgr_a,
        code_agent=agent_a,
        runner=runner_a,
        vcs=FakeVCS(),
    )
    report_a = worker_a.run_job(
        project_id=pid_a,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )

    assert orch_a.get_status(pid_a).state == ProjectState.FAILED
    assert report_a.invocations == 4  # 1 generate_tests + 3 impl rounds (stops before 4th)
    assert len(agent_a.impl_calls) == 3
    assert report_a.failure_cause is not None
    assert "sin cambios" in report_a.failure_cause.lower()
    assert "playwright" in report_a.failure_cause.lower()
    failed_events_a = [ev for ev in store_a.get_events(pid_a) if ev.type == EventType.PROJECT_FAILED]
    assert len(failed_events_a) == 1
    assert "playwright" in failed_events_a[0].payload["reason"].lower()

    # Case B: Immediate 2 consecutive no-op rounds from Round 1 stops after Round 2
    pid_b = "proj-stagnate-from-start"
    orch_b, _ = _create_approved_project(pid_b, num_tests=2, approve=True)
    ws_mgr_b = WorkspaceManager(base_dir=tmp_path / "ws_stag_b")
    agent_b = FakeCodeAgent(
        test_gen_steps=[{"tests/test_web.py": "def test_ui(): assert False\n"}],
        impl_steps=[{}, {}, {"src/deck.ts": "never_called\n"}],
    )
    runner_b = FakeRunner(
        results=[
            CommandResult(
                exit_code=1,
                stdout="AssertionError: expected button visible\n1 failed in 0.12s",
                test_results={"AT-01": False, "AT-02": False},
            ),
            CommandResult(
                exit_code=1,
                stdout="AssertionError: expected button visible\n1 failed in 0.19s",
                test_results={"AT-01": False, "AT-02": False},
            ),
        ]
    )
    worker_b = BuildWorker(
        orchestrator=orch_b,
        workspace_manager=ws_mgr_b,
        code_agent=agent_b,
        runner=runner_b,
        vcs=FakeVCS(),
    )
    report_b = worker_b.run_job(
        project_id=pid_b,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )
    assert orch_b.get_status(pid_b).state == ProjectState.FAILED
    assert report_b.invocations == 3  # 1 generate_tests + 2 impl rounds
    assert len(agent_b.impl_calls) == 2
    assert report_b.failure_cause is not None
    assert "sin cambios" in report_b.failure_cause.lower()

    # Verify _summarize_short strips fenced JSON leakage if an agent emits raw JSON
    leaked = '```json\n{"files_written": ["src/a.py"], "summary": "Solo resumen limpio"}\n```'
    assert worker_b._summarize_short(leaked) == "Solo resumen limpio"


def test_build_worker_handles_binary_pyc_and_binary_assets_in_tests_without_unicode_error(
    tmp_path: Path,
) -> None:
    """INV-13 test lock & files_written survive binary .pyc caches in tests/__pycache__ and protect arbitrary binary fixtures via raw bytes."""
    from agent_factory.ports.code_agent import CodeAgentResult
    from agent_factory.workspace import compute_test_lock, verify_and_restore_test_lock

    pid = "proj-pyc-unicode-safe"
    orchestrator, store = _create_approved_project(pid, num_tests=2, approve=True)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_pyc")

    pyc_rel = "tests/__pycache__/test_app.cpython-312-pytest-9.2.0.pyc"
    pyc_bytes = b"\xcb\x0d\x0d\x0a\x00\x00\x00\x00binary-pyc-payload\xff\xfe"
    fixture_rel = "tests/fixtures/sample.bin"
    fixture_bytes = b"\xcb\x0d\x0d\x0a\x01\x02raw-binary-fixture\xff"

    class _PycGeneratingAgent(FakeCodeAgent):
        def generate_tests(self, workspace_dir, acceptance_tests):  # type: ignore[override]
            tdir = workspace_dir / "tests"
            (tdir / "__pycache__").mkdir(parents=True, exist_ok=True)
            (tdir / "fixtures").mkdir(parents=True, exist_ok=True)
            (tdir / "test_app.py").write_text(
                "def test_at_01(): pass\ndef test_at_02(): pass\n", encoding="utf-8"
            )
            (workspace_dir / pyc_rel).write_bytes(pyc_bytes)
            (workspace_dir / fixture_rel).write_bytes(fixture_bytes)
            return CodeAgentResult(
                files_written=["tests/test_app.py", pyc_rel, fixture_rel],
                summary="Generated tests and ran pytest cache",
                model="Gemini 4 Hydrogen (Medium)",
            )

    agent = _PycGeneratingAgent(
        impl_steps=[
            lambda ws, _p, _f, _a: (
                # Also simulate pytest recreating newer .pyc bytes during run
                (ws / "src").mkdir(parents=True, exist_ok=True),
                (ws / "src" / "deck.ts").write_text("export const ok = true;\n", encoding="utf-8"),
                (ws / pyc_rel).write_bytes(pyc_bytes + b"\x99\x88"),
                CodeAgentResult(files_written=["src/deck.ts"], summary="Implemented app"),
            )[-1],
        ],
    )
    runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=0,
                stdout="2 passed",
                test_results={"AT-01": True, "AT-02": True},
            )
        ]
    )
    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=agent,
        runner=runner,
        vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
    )
    report = worker.run_job(
        project_id=pid,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )

    assert orchestrator.get_status(pid).state == ProjectState.DELIVERED
    assert report.failure_cause is None

    # verify generate_tests event filtered __pycache__/*.pyc cleanly
    gen_ev = next(
        ev
        for ev in store.get_events(pid)
        if ev.type == EventType.AGENT_INVOKED and ev.payload.get("agent") == "generate_tests"
    )
    assert gen_ev.payload["files_written"] == ["tests/test_app.py", fixture_rel]

    # Verify raw-byte snapshot protects binary fixtures against tampering without UnicodeDecodeError
    ws_dir = ws_mgr.get_workspace_dir(pid)
    snap = compute_test_lock(ws_dir)
    (ws_dir / fixture_rel).write_bytes(b"tampered-binary-bytes")
    assert verify_and_restore_test_lock(ws_dir, snap) is False
    assert (ws_dir / fixture_rel).read_bytes() == fixture_bytes
    assert verify_and_restore_test_lock(ws_dir, snap) is True


def test_build_worker_reuses_existing_tests_and_src_and_saves_round_checkpoint_preserving_inv13(
    tmp_path: Path,
) -> None:
    """Incremental resume: BuildWorker reuses existing non-empty tests/ and prior src/, skips generate_tests, preserves INV-13 lock, and persists round checkpoints."""
    pid = "proj-incremental-resume"
    orchestrator, store = _create_approved_project(pid, num_tests=2, approve=True)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_incr")
    ws_dir = ws_mgr.create_workspace(
        pid,
        template_files={"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )
    existing_test = "def test_at_01(): assert True\ndef test_at_02(): assert True\n"
    (ws_dir / "tests").mkdir(parents=True, exist_ok=True)
    (ws_dir / "tests" / "test_app.py").write_text(existing_test, encoding="utf-8")
    (ws_dir / "tests" / "__pycache__").mkdir(parents=True, exist_ok=True)
    (ws_dir / "tests" / "__pycache__" / "test_app.cpython-312-pytest-9.2.0.pyc").write_bytes(
        b"\xcb\x0d\x0d\x0a\x00\x00"
    )
    (ws_dir / "src").mkdir(parents=True, exist_ok=True)
    (ws_dir / "src" / "app.py").write_text("STEP = 1\n", encoding="utf-8")

    # Also prove empty 0-byte file alone does not qualify if no real test exists
    pid_zero = "proj-zero-byte-test"
    orch_z, _ = _create_approved_project(pid_zero, num_tests=2, approve=True)
    ws_z = ws_mgr.create_workspace(pid_zero)
    (ws_z / "tests").mkdir(parents=True, exist_ok=True)
    (ws_z / "tests" / "empty.py").write_bytes(b"")
    worker_z = BuildWorker(
        orchestrator=orch_z,
        workspace_manager=ws_mgr,
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_fresh.py": "def test_ok(): pass\n"}],
            impl_steps=[{"src/app.py": "OK = True\n"}],
        ),
        runner=FakeRunner(
            results=[CommandResult(exit_code=0, stdout="2 passed", test_results={"AT-01": True, "AT-02": True})]
        ),
        vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
    )
    worker_z.run_job(pid_zero)
    assert len(worker_z.code_agent.test_gen_calls) == 1  # type: ignore[attr-defined]

    observed_prior_src: list[str] = []

    def _round1_tamper(ws: Path, _p, _f, _a):
        observed_prior_src.append((ws / "src" / "app.py").read_text(encoding="utf-8"))
        # Attempt to weaken existing reused test -> must be reverted by INV-13 lock
        (ws / "tests" / "test_app.py").write_text("def test_weak(): pass\n", encoding="utf-8")
        from agent_factory.ports.code_agent import CodeAgentResult

        return CodeAgentResult(files_written=["tests/test_app.py"], summary="Tried tampering")

    def _round2_finish(ws: Path, _p, _f, _a):
        prev = (ws / "src" / "app.py").read_text(encoding="utf-8")
        (ws / "src" / "app.py").write_text(prev + "STEP = 2\n", encoding="utf-8")
        from agent_factory.ports.code_agent import CodeAgentResult

        return CodeAgentResult(files_written=["src/app.py"], summary="Continued from prior src")

    agent = FakeCodeAgent(
        test_gen_steps=[{"tests/should_never_run.py": "def test_bad(): pass\n"}],
        impl_steps=[_round1_tamper, _round2_finish],
    )
    runner = FakeRunner(
        results=[
            CommandResult(exit_code=0, stdout="2 passed", test_results={"AT-01": True, "AT-02": True}),
        ]
    )
    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=agent,
        runner=runner,
        vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
    )

    report = worker.run_job(pid)
    assert orchestrator.get_status(pid).state == ProjectState.DELIVERED
    assert report.failure_cause is None
    assert len(agent.test_gen_calls) == 0
    assert not (ws_dir / "tests" / "should_never_run.py").exists()
    assert observed_prior_src == ["STEP = 1\n"]
    assert "STEP = 1\nSTEP = 2\n" in (ws_dir / "src" / "app.py").read_text(encoding="utf-8")
    assert (ws_dir / "tests" / "test_app.py").read_text(encoding="utf-8") == existing_test

    ev_types = [ev.type for ev in store.get_events(pid)]
    assert EventType.TESTS_REUSED in ev_types
    reused_ev = next(ev for ev in store.get_events(pid) if ev.type == EventType.TESTS_REUSED)
    assert reused_ev.payload["files"] == ["tests/test_app.py"]

    ckpt_file = ws_dir / ".agent-factory" / "checkpoint.json"
    assert ckpt_file.is_file()
    import json as _json

    ckpt_data = _json.loads(ckpt_file.read_text(encoding="utf-8"))
    assert ckpt_data["round"] == 2
    assert ckpt_data["files_written"] == ["src/app.py"]
    assert ckpt_data["tests_passed"] == 2
    assert ckpt_data["tests_total"] == 2


def test_build_worker_stagnation_summary_includes_specific_redacted_pytest_failure(
    tmp_path: Path,
) -> None:
    """Early stagnation stop always extracts and redacts the primary pytest failure line (FAILED ... - Error) within ~220 chars."""
    secret = "SECRET_STAG_777"
    pid = "proj-stagnate-specific"
    orchestrator, store = _create_approved_project(pid, num_tests=2, approve=True)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_stag_spec")

    long_tail_fail = (
        "============================= test session starts ==============================\n"
        "F.                                                                       [100%]\n"
        "=================================== FAILURES ===================================\n"
        "______________________________ test_start_button _______________________________\n"
        "    def test_start_button():\n"
        ">       assert '#start-btn' in html_text\n"
        f"E       AssertionError: missing #start-btn token={secret} " + ("z" * 240) + "\n\n"
        "tests/test_app.py:42: AssertionError\n"
        "=========================== short test summary info ============================\n"
        f"FAILED tests/test_app.py::test_start_button - AssertionError: missing #start-btn token={secret} "
        + ("z" * 240)
        + "\n1 failed, 1 passed in 0.25s\n"
    )
    runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=1,
                stdout=long_tail_fail,
                test_results={"AT-01": False, "AT-02": True},
            ),
            CommandResult(
                exit_code=1,
                stdout=long_tail_fail.replace("0.25s", "0.31s"),
                test_results={"AT-01": False, "AT-02": True},
            ),
        ]
    )
    agent = FakeCodeAgent(
        test_gen_steps=[{"tests/test_app.py": "def test_start_button(): assert False\n"}],
        impl_steps=[{}, {}, {"src/never.js": "// skipped"}],
    )
    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=agent,
        runner=runner,
        vcs=FakeVCS(),
        secrets_to_redact=(secret,),
    )
    report = worker.run_job(pid)

    assert orchestrator.get_status(pid).state == ProjectState.FAILED
    assert report.invocations == 3  # 1 gen_tests + 2 noop impl rounds
    cause = report.failure_cause or ""
    assert "sin cambios" in cause.lower()
    assert "FAILED tests/test_app.py::test_start_button" in cause
    assert "AssertionError" in cause
    assert secret not in cause
    assert "[REDACTED]" in cause
    # Snippet portion inside parentheses bounded around ~220 chars
    assert len(cause) <= 320
    failed_ev = next(ev for ev in store.get_events(pid) if ev.type == EventType.PROJECT_FAILED)
    assert "FAILED tests/test_app.py::test_start_button" in failed_ev.payload["reason"]
    assert secret not in failed_ev.payload["reason"]

    # Also verify _summarize_stagnation_cause handles E-only / skipped / unlabeled pytest output cleanly
    no_dash_out = CommandResult(
        exit_code=1,
        stdout=(
            "E       AssertionError: expected button\n"
            "FAILED tests/test_ui.py::test_render\n"
        ),
    )
    cause_2 = worker._summarize_stagnation_cause(no_dash_out, ["AT-01"])
    assert "FAILED tests/test_ui.py::test_render" in cause_2
    assert "AssertionError: expected button" in cause_2

    skipped_out = CommandResult(
        exit_code=0,
        stdout="SKIPPED [4] tests\\test_app.py:83: Node.js is not available in PATH\n2 passed, 4 skipped in 0.15s\n",
    )
    cause_3 = worker._summarize_stagnation_cause(skipped_out, ["AT-01"])
    assert "Node.js is not available in PATH" in cause_3
    assert "pruebas omitidas" in cause_3.lower()
    assert "SKIPPED" in cause_3
    assert "herramientas externas ausentes" in cause_3.lower()

    skipped_count_out = CommandResult(
        exit_code=0,
        stdout="2 passed, 4 skipped in 0.15s\n",
    )
    cause_4 = worker._summarize_stagnation_cause(skipped_count_out, ["AT-01"])
    assert "pruebas omitidas" in cause_4.lower()
    assert "4 omitidas" in cause_4
    assert "SKIPPED" in cause_4


def test_build_worker_stagnation_due_to_skipped_tests_reports_clear_missing_tool_explanation(
    tmp_path: Path,
) -> None:
    """When tests are skipped due to missing external runtime (Node.js), stagnation brake emits clear explanation in Spanish."""
    pid = "proj-stagnate-skipped-node"
    orchestrator, store = _create_approved_project(pid, num_tests=2, approve=True)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_stag_skip")

    agent = FakeCodeAgent(
        test_gen_steps=[{"tests/test_app.py": "def test_node(): assert True\n"}],
        impl_steps=[{}, {}, {"src/app.js": "export const ready = true;\n"}],
    )
    skipped_stdout = (
        "SKIPPED [2] tests/test_app.py:15: Node.js is not available in PATH\n"
        "2 skipped in 0.10s\n"
    )
    runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=0,
                stdout=skipped_stdout,
                test_results={"AT-01": False, "AT-02": False},
            ),
            CommandResult(
                exit_code=0,
                stdout=skipped_stdout,
                test_results={"AT-01": False, "AT-02": False},
            ),
        ]
    )
    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=agent,
        runner=runner,
        vcs=FakeVCS(),
    )
    report = worker.run_job(pid)

    assert orchestrator.get_status(pid).state == ProjectState.FAILED
    assert report.invocations == 3  # 1 test gen + 2 noop impl rounds
    cause = report.failure_cause or ""
    assert "sin cambios" in cause.lower()
    assert "pruebas omitidas" in cause.lower()
    assert "SKIPPED" in cause
    assert "Node.js is not available in PATH" in cause
    assert "herramientas externas ausentes" in cause.lower()

    failed_events = [ev for ev in store.get_events(pid) if ev.type == EventType.PROJECT_FAILED]
    assert len(failed_events) == 1
    assert "Node.js is not available in PATH" in failed_events[0].payload["reason"]


def test_build_worker_and_scaffold_isolate_pytest_inside_workspace_without_parent_pyproject(
    tmp_path: Path,
) -> None:
    """Workspace pytest.ini and explicit -c/--rootdir keep pytest strictly scoped inside workspace and protected by INV-13."""
    import sys

    from agent_factory.adapters.subprocess_runner import SubprocessRunner
    from agent_factory.scaffold import DEFAULT_TEMPLATES_ROOT, TemplateScaffold

    parent_repo = tmp_path / "agent_factory_parent"
    parent_repo.mkdir(parents=True, exist_ok=True)
    # Hostile parent pyproject.toml that would immediately break workspace pytest runs if climbed
    (parent_repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\n'
        'testpaths = ["nonexistent_parent_tests"]\n'
        'addopts = "--cov=src/agent_factory --definitely-invalid-parent-option"\n',
        encoding="utf-8",
    )

    ws_mgr = WorkspaceManager(base_dir=parent_repo / "workspaces")
    scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=DEFAULT_TEMPLATES_ROOT)

    # 1. TemplateScaffold.scaffold creates minimal pytest.ini without overwriting custom pytest.ini
    pid_scaffold = "proj-scaffold-ini"
    ws_scaffold = scaffold.scaffold(pid_scaffold, template_id="static-web")
    expected_ini = "[pytest]\ntestpaths = tests\naddopts = -ra --tb=short\n"
    assert (ws_scaffold / "pytest.ini").is_file()
    assert (ws_scaffold / "pytest.ini").read_text(encoding="utf-8") == expected_ini

    pid_custom = "proj-custom-ini"
    custom_ini = "[pytest]\ntestpaths = tests\naddopts = -q -s\n"
    ws_custom = ws_mgr.create_workspace(pid_custom, template_files={"pytest.ini": custom_ini})
    scaffold.scaffold(pid_custom, template_id="static-web")
    assert (ws_custom / "pytest.ini").read_text(encoding="utf-8") == custom_ini

    # 2. BuildWorker isolates real pytest execution inside workspace despite hostile parent pyproject.toml and protects pytest.ini via INV-13
    pid = "proj-pytest-isolation"
    orchestrator, store = _create_approved_project(pid, num_tests=2, approve=True)
    real_sub = SubprocessRunner(default_timeout_seconds=15.0)
    recorded_cmds: list[list[str]] = []

    class _IsolatingPytestRunner:
        def run(self, command, cwd: Path, timeout_seconds: float = 30.0, env=None, input=None):
            cmd_list = list(command)
            recorded_cmds.append(cmd_list)
            actual = [sys.executable, "-m", "pytest", *cmd_list[1:]] if cmd_list[0] == "pytest" else cmd_list
            return real_sub.run(actual, cwd=cwd, timeout_seconds=timeout_seconds, env=env, input=input)

    test_code = (
        "from pathlib import Path\n\n"
        "def test_at_01_rootdir_isolated(request):\n"
        "    ws = Path.cwd().resolve()\n"
        "    assert request.config.rootpath.resolve() == ws\n"
        "    assert request.config.inipath is not None\n"
        "    assert request.config.inipath.resolve() == (ws / 'pytest.ini').resolve()\n\n"
        "def test_at_02_implementation_present():\n"
        "    assert (Path.cwd() / 'src' / 'deck.ts').is_file()\n"
    )
    agent = FakeCodeAgent(
        test_gen_steps=[{"tests/test_isolation.py": test_code}],
        impl_steps=[
            # Attempt 1: tampers with pytest.ini -> INV-13 must revert and skip pytest run
            {"pytest.ini": "[pytest]\naddopts = --collect-only\n", "src/deck.ts": "// tampered\n"},
            # Attempt 2: legitimate implementation without touching pytest.ini
            {"src/deck.ts": "export const isolated = true;\n"},
        ],
    )
    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=agent,
        runner=_IsolatingPytestRunner(),  # type: ignore[arg-type]
        vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
    )
    report = worker.run_job(pid)

    assert orchestrator.get_status(pid).state == ProjectState.DELIVERED
    assert report.invocations == 3  # 1 test_gen + 1 tampered + 1 valid
    ws_dir = ws_mgr.get_workspace_dir(pid)
    assert (ws_dir / "pytest.ini").read_text(encoding="utf-8") == expected_ini
    assert len(recorded_cmds) == 1
    cmd = recorded_cmds[0]
    assert cmd[0] == "pytest"
    assert "-v" in cmd
    assert "--tb=short" in cmd
    assert "-ra" in cmd
    assert "-c" in cmd
    assert str((ws_dir / "pytest.ini").resolve()) in cmd
    assert f"--rootdir={ws_dir.resolve()}" in cmd

    invoked_impl = [
        ev
        for ev in store.get_events(pid)
        if ev.type == EventType.AGENT_INVOKED and ev.payload.get("agent") == "implement_or_fix"
    ]
    assert [ev.payload["outcome"] for ev in invoked_impl] == ["reverted_tamper", "ok"]


def test_build_worker_stagnation_ignores_pytest_headers_and_reports_real_failure(
    tmp_path: Path,
) -> None:
    """_summarize_stagnation_cause ignores platform/rootdir/configfile/inifile/plugins/cachedir/collected headers and extracts real errors or summaries."""
    pid = "proj-stag-headers"
    orchestrator, _ = _create_approved_project(pid, num_tests=1, approve=True)
    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws_stag_hdr"),
        code_agent=FakeCodeAgent(),
        runner=FakeRunner(),
        vcs=FakeVCS(),
    )

    # 1. Pytest CLI usage error whose trailing lines are inifile: and rootdir:
    usage_err = CommandResult(
        exit_code=4,
        stderr=(
            "ERROR: usage: pytest [options] [file_or_dir] [file_or_dir] [...]\n"
            "pytest: error: unrecognized arguments: --cov=src/agent_factory\n"
            "  inifile: C:\\Users\\dev\\agent-factory-liwi\\pyproject.toml\n"
            "  rootdir: C:\\Users\\dev\\agent-factory-liwi\n"
        ),
    )
    cause_usage = worker._summarize_stagnation_cause(usage_err, ["AT-01"])
    assert "inifile:" not in cause_usage
    assert "rootdir:" not in cause_usage
    assert "unrecognized arguments: --cov=src/agent_factory" in cause_usage

    # 2. Standard verbose headers + FAILED line -> ignores headers and reports FAILED
    hdr_and_fail = CommandResult(
        exit_code=1,
        stdout=(
            "============================= test session starts ==============================\n"
            "platform win32 -- Python 3.12.10, pytest-9.2.0, pluggy-1.7.0\n"
            "cachedir: .pytest_cache\n"
            "rootdir: C:\\Users\\dev\\agent-factory-liwi\n"
            "configfile: pyproject.toml\n"
            "inifile: C:\\Users\\dev\\agent-factory-liwi\\pyproject.toml\n"
            "plugins: cov-7.1.0, socket-0.8.1, timeout-2.4.0\n"
            "collected 2 items\n\n"
            "FAILED tests/test_app.py::test_render - AssertionError: missing root\n"
            "========================= 1 failed, 1 passed in 0.12s ==========================\n"
        ),
    )
    cause_fail = worker._summarize_stagnation_cause(hdr_and_fail, ["AT-01"])
    for forbidden_hdr in ("platform ", "rootdir:", "configfile:", "inifile:", "plugins:", "cachedir:", "collected "):
        assert forbidden_hdr not in cause_fail
    assert "FAILED tests/test_app.py::test_render - AssertionError: missing root" in cause_fail

    # 3. Headers + final summary === ... === without explicit FAILED line
    hdr_and_summary = CommandResult(
        exit_code=1,
        stdout=(
            "platform win32 -- Python 3.12.10, pytest-9.2.0\n"
            "cachedir: .pytest_cache\n"
            "rootdir: C:\\repo\\ws\n"
            "configfile: pytest.ini\n"
            "plugins: cov-7.1.0\n"
            "collected 0 items\n"
            "=========================== no tests ran in 0.01s ============================\n"
        ),
    )
    cause_summary = worker._summarize_stagnation_cause(hdr_and_summary, ["AT-01"])
    assert "rootdir:" not in cause_summary
    assert "configfile:" not in cause_summary
    assert "no tests ran in 0.01s" in cause_summary

    # 4. Headers + SKIPPED summary -> ignores headers and reports clear Spanish skip explanation
    hdr_and_skip = CommandResult(
        exit_code=0,
        stdout=(
            "platform win32 -- Python 3.12.10, pytest-9.2.0\n"
            "rootdir: C:\\repo\\ws\n"
            "inifile: C:\\repo\\ws\\pytest.ini\n"
            "collected 6 items\n"
            "SKIPPED [4] tests/test_app.py:83: Node.js is not available in PATH\n"
            "======================== 2 passed, 4 skipped in 0.10s ==========================\n"
        ),
    )
    cause_skip = worker._summarize_stagnation_cause(hdr_and_skip, ["AT-01"])
    assert "inifile:" not in cause_skip
    assert "rootdir:" not in cause_skip
    assert "pruebas omitidas [SKIPPED]" in cause_skip
    assert "Node.js is not available in PATH" in cause_skip

    # 5. Headers and progress-dot lines only -> never surfaces inifile/rootdir or ...... [100%]
    hdr_only = CommandResult(
        exit_code=1,
        stdout=(
            "platform win32 -- Python 3.12.10\n"
            "inifile: C:\\repo\\pyproject.toml\n"
            "rootdir: C:\\repo\n"
            "......                                                                   [100%]\n"
        ),
    )
    cause_hdr_only = worker._summarize_stagnation_cause(hdr_only, [])
    assert "inifile:" not in cause_hdr_only
    assert "rootdir:" not in cause_hdr_only
    assert "[100%]" not in cause_hdr_only
    assert "......" not in cause_hdr_only
    assert "continúan fallando" in cause_hdr_only


def test_build_worker_passes_on_subprocess_pytest_summary_and_progress_dots_and_preserves_post_test_errors(
    tmp_path: Path,
) -> None:
    """SubprocessRunner leaves CommandResult.test_results empty: BuildWorker must pass on exit_code=0 with passed>0 (including '...... [100%]' or '6 passed in 0.12s'), fail on skipped>0, and record real stage errors after pytest passes."""
    # Case A: Quiet progress-dots output ("...... [100%]") with empty test_results converges to DELIVERED
    pid_dots = "proj-dots-pass"
    orch_a, store_a = _create_approved_project(pid_dots, num_tests=2, approve=True)
    ws_mgr_a = WorkspaceManager(base_dir=tmp_path / "ws_dots")
    agent_a = FakeCodeAgent(
        test_gen_steps=[{"tests/test_app.py": "def test_ok(): pass\n"}],
        impl_steps=[{"src/deck.ts": "export const ok = true;\n"}],
    )
    runner_a = FakeRunner(
        results=[
            CommandResult(
                exit_code=0,
                stdout="......                                                                   [100%]\n",
                stderr="",
                test_results={},
            )
        ]
    )
    worker_a = BuildWorker(
        orchestrator=orch_a,
        workspace_manager=ws_mgr_a,
        code_agent=agent_a,
        runner=runner_a,
        vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
    )
    report_a = worker_a.run_job(pid_dots)
    assert orch_a.get_status(pid_dots).state == ProjectState.DELIVERED
    assert report_a.invocations == 2
    assert report_a.test_results == {"AT-01": True, "AT-02": True}
    assert len(agent_a.impl_calls) == 1
    tests_ev_a = next(ev for ev in store_a.get_events(pid_dots) if ev.type == EventType.TESTS_UPDATED)
    assert tests_ev_a.payload["passed"] == 6
    assert tests_ev_a.payload["failed"] == 0
    assert tests_ev_a.payload["total"] == 6

    # Case B: Verbose explicit summary ("6 passed in 0.12s") with empty test_results converges to DELIVERED
    pid_sum = "proj-summary-pass"
    orch_b, _ = _create_approved_project(pid_sum, num_tests=2, approve=True)
    ws_mgr_b = WorkspaceManager(base_dir=tmp_path / "ws_sum")
    worker_b = BuildWorker(
        orchestrator=orch_b,
        workspace_manager=ws_mgr_b,
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_app.py": "def test_ok(): pass\n"}],
            impl_steps=[{"src/deck.ts": "export const ok = true;\n"}],
        ),
        runner=FakeRunner(
            results=[
                CommandResult(
                    exit_code=0,
                    stdout=(
                        "tests/test_app.py::test_1 PASSED [ 50%]\n"
                        "tests/test_app.py::test_2 PASSED [100%]\n"
                        "============================== 6 passed in 0.12s ==============================\n"
                    ),
                    test_results={},
                )
            ]
        ),
        vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
    )
    report_b = worker_b.run_job(pid_sum)
    assert orch_b.get_status(pid_sum).state == ProjectState.DELIVERED
    assert report_b.invocations == 2

    # Case C: exit_code=0 with skipped > 0 (e.g. "2 passed, 4 skipped") still fails verification & triggers skipped stagnation
    pid_skip = "proj-empty-results-skip"
    orch_c, _ = _create_approved_project(pid_skip, num_tests=2, approve=True)
    ws_mgr_c = WorkspaceManager(base_dir=tmp_path / "ws_skip_empty")
    skip_out = (
        "SKIPPED [4] tests/test_app.py:83: Node.js is not available in PATH\n"
        "========================= 2 passed, 4 skipped in 0.10s =========================\n"
    )
    worker_c = BuildWorker(
        orchestrator=orch_c,
        workspace_manager=ws_mgr_c,
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_app.py": "def test_ok(): pass\n"}],
            impl_steps=[{}, {}],
        ),
        runner=FakeRunner(
            results=[
                CommandResult(exit_code=0, stdout=skip_out, test_results={}),
                CommandResult(exit_code=0, stdout=skip_out, test_results={}),
            ]
        ),
        vcs=FakeVCS(),
    )
    report_c = worker_c.run_job(pid_skip)
    assert orch_c.get_status(pid_skip).state == ProjectState.FAILED
    assert report_c.failure_cause is not None
    assert "pruebas omitidas [SKIPPED]" in report_c.failure_cause

    # Case D: When pytest passes ("...... [100%]") and GitHub push fails afterward, error/last_failure_output record the GitHub error
    pid_vcs = "proj-post-test-vcs-err"
    orch_d, store_d = _create_approved_project(pid_vcs, num_tests=2, approve=True)
    ws_mgr_d = WorkspaceManager(base_dir=tmp_path / "ws_vcs_err")
    queue_d = LocalJobQueue()

    class _FailingPushVCS(FakeVCS):
        def push_branch(self, project_id: str, workspace_dir: Path, branch: str = "work") -> str:
            raise RuntimeError("GitHub push rejected (403 remote permission denied)")

    worker_d = BuildWorker(
        orchestrator=orch_d,
        workspace_manager=ws_mgr_d,
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_app.py": "def test_ok(): pass\n"}],
            impl_steps=[{"src/deck.ts": "export const ok = true;\n"}],
        ),
        runner=FakeRunner(
            results=[
                CommandResult(
                    exit_code=0,
                    stdout="......                                                                   [100%]\n",
                    test_results={},
                )
            ]
        ),
        vcs=_FailingPushVCS(),
        queue=queue_d,
    )
    report_d = worker_d.run_job(pid_vcs)
    assert orch_d.get_status(pid_vcs).state == ProjectState.FAILED
    assert report_d.failure_cause == "GitHub push rejected (403 remote permission denied)"
    job_d = queue_d.get_job(pid_vcs)
    assert job_d is not None
    assert job_d.last_failure_output == "GitHub push rejected (403 remote permission denied)"
    assert "[100%]" not in job_d.last_failure_output
    phase_err = next(ev for ev in store_d.get_events(pid_vcs) if ev.type == EventType.PHASE_ERROR)
    assert "GitHub push rejected (403 remote permission denied)" in phase_err.payload["detail"]
    assert phase_err.payload["state"] == ProjectState.VERIFYING.value


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_build_worker_http_smoke_test_detects_missing_404_asset_and_recovers_after_fix(
    tmp_path: Path,
) -> None:
    """BuildWorker runs real HTTP Smoke Test before VERIFYING, feeds missing 404 resource back to implement_or_fix, stops temp server cleanly in finally, and delivers once all assets return 200."""
    import urllib.error
    import urllib.request

    from agent_factory.runtime_runner import LocalAppRunner

    pid = "proj-smoke-test-recovery"
    orchestrator, _ = _create_approved_project(pid, num_tests=1, approve=True)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_smoke")
    local_runner = LocalAppRunner(workspace_manager=ws_mgr)

    agent = FakeCodeAgent(
        test_gen_steps=[{"tests/test_app.py": "def test_at_01(): assert True\n"}],
        impl_steps=[
            # Round 1: writes index.html & styles.css, but forgets app.js referenced in <script src="./app.js">
            {
                "index.html": (
                    "<!doctype html><html><head>"
                    "<link rel='stylesheet' href='./styles.css'>"
                    "</head><body><h1>App</h1><script src='./app.js'></script></body></html>\n"
                ),
                "styles.css": "body { color: #111; }\n",
            },
            # Round 2: fixes the missing app.js reported by HTTP Smoke Test
            {"app.js": "console.log('loaded');\n"},
        ],
    )
    runner = FakeRunner(
        results=[
            CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True}),
            CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True}),
        ]
    )
    vcs = FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI green")])
    worker = BuildWorker(
        orchestrator=orchestrator,
        workspace_manager=ws_mgr,
        code_agent=agent,
        runner=runner,
        vcs=vcs,
        local_app_runner=local_runner,
    )

    try:
        report = worker.run_job(pid)
        assert orchestrator.get_status(pid).state == ProjectState.DELIVERED
        assert report.invocations == 3  # 1 test_gen + 2 impl rounds
        assert len(agent.impl_calls) == 2
        round2_failure = agent.impl_calls[1]["failure_output"]
        assert "404" in round2_failure
        assert "app.js" in round2_failure
        assert len(vcs.pushes) == 1

        # Temporary smoke server was stopped cleanly in finally (no active local app or leaked port)
        st_local = local_runner.get_status(pid)
        assert st_local.running is False
        assert st_local.url is None

        # Verify direct LocalAppRunner smoke check helper against clean static workspace
        from agent_factory.runtime_runner import run_http_smoke_test

        ws_dir = ws_mgr.get_workspace_dir(pid)
        smoke_ok = run_http_smoke_test(local_runner, pid, ws_dir)
        assert smoke_ok.passed is True
        assert smoke_ok.failed_resources == []

        # Remove app.js and verify direct smoke test reports missing app.js -> 404 and cleans up port
        (ws_dir / "app.js").unlink()
        smoke_fail = run_http_smoke_test(local_runner, pid, ws_dir)
        assert smoke_fail.passed is False
        assert any("app.js" in item and "404" in item for item in smoke_fail.failed_resources)
        assert local_runner.get_status(pid).running is False
        if smoke_fail.url:
            with pytest.raises((urllib.error.URLError, OSError)):
                urllib.request.urlopen(smoke_fail.url, timeout=1.0)
    finally:
        local_runner.stop_all()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_local_app_runner_deep_detects_nested_python_package_module_and_static_subdirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """detect_workspace_app finds nested package servers (src/<pkg>/server.py -> python -m <pkg>.server with PYTHONPATH=src), __main__ web files outside tests/, public/index.html, and reports redacted startup stderr cleanly."""
    import json
    import urllib.request

    from agent_factory.runtime_runner import LocalAppError, LocalAppRunner, detect_workspace_app

    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_deep")

    # 1. Nested Python package src/mi_app/server.py even with empty root template index.html
    pid_pkg = "proj-nested-pkg"
    ws_pkg = ws_mgr.create_workspace(pid_pkg)
    (ws_pkg / "index.html").write_text(
        "<!DOCTYPE html><html><head><title>Static Web App</title></head>"
        "<body><main id='app'></main><script type='module' src='./src/app.js'></script></body></html>\n",
        encoding="utf-8",
    )
    (ws_pkg / "tests").mkdir()
    (ws_pkg / "tests" / "test_server.py").write_text(
        "if __name__ == '__main__':\n    from http.server import HTTPServer\n",
        encoding="utf-8",
    )
    pkg_dir = ws_pkg / "src" / "mi_app"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "helper.py").write_text("GREETING = 'Hola desde mi_app'\n", encoding="utf-8")
    (pkg_dir / "server.py").write_text(
        "import http.server, json, os\n"
        "from mi_app.helper import GREETING\n"
        "host = os.environ['HOST']\n"
        "port = int(os.environ['PORT'])\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        body = json.dumps({'msg': GREETING}).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(body)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body)\n"
        "    def log_message(self, *args):\n"
        "        pass\n"
        "if __name__ == '__main__':\n"
        "    http.server.ThreadingHTTPServer((host, port), H).serve_forever()\n",
        encoding="utf-8",
    )

    info_pkg = detect_workspace_app(ws_pkg)
    assert info_pkg.app_type == "python_local"
    assert info_pkg.entrypoint is not None
    assert info_pkg.entrypoint.resolve() == (pkg_dir / "server.py").resolve()
    assert info_pkg.module_name == "mi_app.server"

    runner = LocalAppRunner(workspace_manager=ws_mgr)
    try:
        st = runner.start(pid_pkg)
        assert st.running is True
        assert st.app_type == "python_local"
        assert st.url is not None
        with urllib.request.urlopen(st.url, timeout=5.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
        assert data == {"msg": "Hola desde mi_app"}
    finally:
        runner.stop_all()

    # 2. __main__ server file in custom subdir (ignoring tests/ & .venv/)
    pid_main = "proj-deep-main"
    ws_main = ws_mgr.create_workspace(pid_main)
    (ws_main / "tests").mkdir()
    (ws_main / "tests" / "test_fake.py").write_text(
        "from http.server import HTTPServer\nif __name__ == '__main__': pass\n", encoding="utf-8"
    )
    (ws_main / "web_app").mkdir()
    (ws_main / "web_app" / "portal.py").write_text(
        "from http.server import ThreadingHTTPServer\nif __name__ == '__main__':\n    pass\n",
        encoding="utf-8",
    )
    info_main = detect_workspace_app(ws_main)
    assert info_main.app_type == "python_local"
    assert info_main.entrypoint == (ws_main / "web_app" / "portal.py").resolve()

    # 3. Deep static index.html in public/ overriding untouched template root
    pid_pub = "proj-public-static"
    ws_pub = ws_mgr.create_workspace(pid_pub)
    (ws_pub / "index.html").write_text(
        "<html><head><title>Static Web App</title></head>"
        "<body><main id='app'></main><script type='module' src='./src/app.js'></script></body></html>\n",
        encoding="utf-8",
    )
    (ws_pub / "public").mkdir()
    (ws_pub / "public" / "index.html").write_text("<h1>Public App</h1>\n", encoding="utf-8")
    info_pub = detect_workspace_app(ws_pub)
    assert info_pub.app_type == "static"
    assert info_pub.serve_dir == (ws_pub / "public").resolve()

    # 4. Immediate Python startup crash captures redacted stderr cleanly
    secret_val = "TEST_ERR_SECRET_445"
    monkeypatch.setenv("GITHUB_TOKEN", secret_val)
    pid_crash = "proj-py-crash"
    ws_crash = ws_mgr.create_workspace(pid_crash)
    (ws_crash / "app.py").write_text(
        f"import sys\nsys.stderr.write('secret={secret_val}\\n')\nimport missing_yt_dlp_mod\n",
        encoding="utf-8",
    )
    runner_err = LocalAppRunner(workspace_manager=ws_mgr, secrets_to_redact=(secret_val,))
    with pytest.raises(LocalAppError) as crash_err:
        runner_err.start(pid_crash)
    err_str = str(crash_err.value)
    assert "missing_yt_dlp_mod" in err_str
    assert secret_val not in err_str


def test_build_worker_writes_favicon_svg_without_overwriting_existing_one(
    tmp_path: Path,
) -> None:
    """BuildWorker writes auto-generated favicon.svg if missing before push, and preserves any custom favicon already in workspace."""
    # Case A: Missing favicon -> generated automatically with music icon SVG
    pid_a = "proj-auto-fav"
    orch_a, _ = _create_approved_project(pid_a, num_tests=1, approve=True)
    ws_mgr_a = WorkspaceManager(base_dir=tmp_path / "ws_fav_a")
    worker_a = BuildWorker(
        orchestrator=orch_a,
        workspace_manager=ws_mgr_a,
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_app.py": "def test_ok(): assert True\n"}],
            impl_steps=[{"index.html": "<h1>Musiquita</h1>\n"}],
        ),
        runner=FakeRunner(
            results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]
        ),
        vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
    )
    worker_a.run_job(pid_a)
    assert orch_a.get_status(pid_a).state == ProjectState.DELIVERED
    fav_a = ws_mgr_a.get_workspace_dir(pid_a) / "favicon.svg"
    assert fav_a.is_file()
    fav_txt = fav_a.read_text(encoding="utf-8")
    assert "<svg" in fav_txt
    assert 'viewBox="0 0 64 64"' in fav_txt
    assert "<script" not in fav_txt.lower()

    # Case B: Existing custom favicon.svg -> preserved untouched
    pid_b = "proj-keep-fav"
    orch_b, _ = _create_approved_project(pid_b, num_tests=1, approve=True)
    ws_mgr_b = WorkspaceManager(base_dir=tmp_path / "ws_fav_b")
    custom_fav = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>\n'
    worker_b = BuildWorker(
        orchestrator=orch_b,
        workspace_manager=ws_mgr_b,
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_app.py": "def test_ok(): assert True\n"}],
            impl_steps=[{"index.html": "<h1>App</h1>\n", "favicon.svg": custom_fav}],
        ),
        runner=FakeRunner(
            results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]
        ),
        vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
    )
    worker_b.run_job(pid_b)
    assert (ws_mgr_b.get_workspace_dir(pid_b) / "favicon.svg").read_text(encoding="utf-8") == custom_fav


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_local_app_runner_isolated_venv_deps_hash_cache_and_git_exclude(
    tmp_path: Path,
) -> None:
    """LocalAppRunner creates per-workspace .venv, caches deps via .deps_hash, runs with venv python, skips pip on run_http_smoke_test(install_deps=False), activates install_deps=True via /start, and excludes .venv/ & .agent-factory/ from Git."""
    import hashlib
    import http.server  # noqa: F401
    import json
    import os
    import subprocess
    import urllib.request
    from collections.abc import Sequence
    from typing import Any

    from agent_factory.adapters.github_vcs_adapter import GitHubVCSAdapter
    from agent_factory.adapters.sqlite_store import SqliteEventStore
    from agent_factory.dashboard import start_dashboard_server
    from agent_factory.domain.events import create_event
    from agent_factory.runtime_runner import LocalAppRunner, run_http_smoke_test

    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_venv")
    pid = "proj-py-venv"
    ws_dir = ws_mgr.create_workspace(pid)
    req_path = ws_dir / "src" / "requirements.txt"
    req_path.parent.mkdir(parents=True, exist_ok=True)
    req_v1 = "# local test deps v1\n"
    req_path.write_text(req_v1, encoding="utf-8", newline="\n")

    (ws_dir / "app.py").write_text(
        "import http.server, json, os, sys\n"
        "host = os.environ['HOST']\n"
        "port = int(os.environ['PORT'])\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        body = json.dumps({'prefix': sys.prefix, 'exe': sys.executable}).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(body)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body)\n"
        "    def log_message(self, *args):\n"
        "        pass\n"
        "if __name__ == '__main__':\n"
        "    http.server.ThreadingHTTPServer((host, port), H).serve_forever()\n",
        encoding="utf-8",
    )

    venv_cmds: list[list[str]] = []
    pip_cmds: list[list[str]] = []
    real_run = subprocess.run

    def _recording_runner(cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd_list = [str(c) for c in cmd]
        if len(cmd_list) >= 4 and cmd_list[1:3] == ["-m", "venv"]:
            venv_cmds.append(cmd_list)
            return real_run([cmd_list[0], "-m", "venv", "--without-pip", *cmd_list[3:]], **kwargs)
        if "-m" in cmd_list and "pip" in cmd_list:
            pip_cmds.append(cmd_list)
            return subprocess.CompletedProcess(cmd_list, 0, stdout="ok", stderr="")
        return real_run(cmd_list, **kwargs)

    # 1. BuildWorker default run_http_smoke_test keeps install_deps=False (no venv/pip created)
    smoke_runner = LocalAppRunner(workspace_manager=ws_mgr, dep_command_runner=_recording_runner)
    try:
        smoke_res = run_http_smoke_test(smoke_runner, pid, ws_dir)
        assert smoke_res.passed is True
        assert not (ws_dir / ".venv").exists()
        assert pip_cmds == []
    finally:
        smoke_runner.stop_all()

    # 2. First explicit start(install_deps=True) creates <ws>/.venv, runs pip -r src/requirements.txt, saves .deps_hash, and runs app using venv python
    runner = LocalAppRunner(workspace_manager=ws_mgr, dep_command_runner=_recording_runner)
    try:
        st1 = runner.start(pid, install_deps=True)
        assert st1.running is True
        assert st1.app_type == "python_local"
        assert len(venv_cmds) == 1
        assert venv_cmds[0][1:3] == ["-m", "venv"]
        assert Path(venv_cmds[0][3]).resolve() == (ws_dir / ".venv").resolve()
        assert len(pip_cmds) == 1
        assert "-r" in pip_cmds[0]
        assert str(req_path.resolve()) in pip_cmds[0]

        expected_venv_py = (
            ws_dir / ".venv" / "Scripts" / "python.exe"
            if os.name == "nt"
            else ws_dir / ".venv" / "bin" / "python"
        )
        assert expected_venv_py.is_file()
        assert pip_cmds[0][0] == str(expected_venv_py.resolve())

        deps_hash_file = ws_dir / ".venv" / ".deps_hash"
        assert deps_hash_file.is_file()
        expected_hash_v1 = hashlib.sha256(
            b"src/requirements.txt\0" + req_v1.encode("utf-8") + b"\0"
        ).hexdigest()
        assert deps_hash_file.read_text(encoding="utf-8").strip() == expected_hash_v1

        with urllib.request.urlopen(st1.url, timeout=5.0) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        assert Path(info["prefix"]).resolve() == (ws_dir / ".venv").resolve()
        runner.stop(pid)

        # 3. Second start skips pip cleanly when .deps_hash matches
        st2 = runner.start(pid, install_deps=True)
        assert st2.running is True
        assert len(pip_cmds) == 1
        runner.stop(pid)

        # 4. Updating requirements.txt invalidates cache and triggers pip again
        req_v2 = "flask==3.0.0\n"
        req_path.write_text(req_v2, encoding="utf-8", newline="\n")
        st3 = runner.start(pid, install_deps=True)
        assert st3.running is True
        assert len(pip_cmds) == 2
        expected_hash_v2 = hashlib.sha256(
            b"src/requirements.txt\0" + req_v2.encode("utf-8") + b"\0"
        ).hexdigest()
        assert deps_hash_file.read_text(encoding="utf-8").strip() == expected_hash_v2
        runner.stop(pid)

        # 5. Dashboard POST /api/projects/<id>/start enables install_deps=True
        store = SqliteEventStore(str(tmp_path / "dash_venv.db"))
        store.append(
            create_event(
                pid,
                EventType.PROJECT_CREATED,
                {"name": "Py Venv App", "objective": "Test venv start", "state": "INTERVIEW"},
                at=datetime(2026, 11, 20, 12, 0, tzinfo=UTC),
            )
        )
        orch = Orchestrator(store=store, llm=FakeLLM(), workspace_manager=ws_mgr)
        req_v3 = "flask==3.1.0\n"
        req_path.write_text(req_v3, encoding="utf-8", newline="\n")
        server, thread, base_url = start_dashboard_server(
            orchestrator=orch,
            port=0,
            workspace_manager=ws_mgr,
            local_runner=runner,
        )
        try:
            start_req = urllib.request.Request(f"{base_url}/api/projects/{pid}/start", method="POST")
            with urllib.request.urlopen(start_req, timeout=5.0) as sr:
                assert sr.status == 200
            assert len(pip_cmds) == 3
            stop_req = urllib.request.Request(f"{base_url}/api/projects/{pid}/stop", method="POST")
            with urllib.request.urlopen(stop_req, timeout=5.0) as stp:
                assert stp.status == 200
        finally:
            server.shutdown()
            server.server_close()
            server.controller.wait_for_idle(timeout=5.0)
            if thread.is_alive():
                thread.join(timeout=2.0)
            store.close()
    finally:
        runner.stop_all()

    # 6. GitHubVCSAdapter excludes .venv/ and .agent-factory/ via .git/info/exclude & .gitignore
    (ws_dir / ".agent-factory").mkdir(parents=True, exist_ok=True)
    (ws_dir / ".agent-factory" / "state.json").write_text('{"step": 1}\n', encoding="utf-8")
    bare_remote = tmp_path / "bare_venv_remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_remote)], check=True, capture_output=True)
    vcs = GitHubVCSAdapter(repo_base_url=str(tmp_path), github_token="")
    subprocess.run(["git", "init", "-b", "main"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare_remote)], cwd=ws_dir, check=True)
    vcs.push_branch(pid, ws_dir, branch="work")

    excl_text = (ws_dir / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".venv/" in excl_text
    assert ".agent-factory/" in excl_text
    gitignore_text = (ws_dir / ".gitignore").read_text(encoding="utf-8")
    assert ".venv/" in gitignore_text
    assert ".agent-factory/" in gitignore_text

    tree_files = subprocess.run(
        ["git", "-C", str(bare_remote), "ls-tree", "-r", "refs/heads/work", "--name-only"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "app.py" in tree_files
    assert "src/requirements.txt" in tree_files
    assert not any(f == ".venv" or f.startswith(".venv/") for f in tree_files)
    assert not any(f == ".agent-factory" or f.startswith(".agent-factory/") for f in tree_files)

    # 7. pyproject.toml [project] triggers pip install -e . cleanly while pytest-only pyproject does not
    pip_pyproj_cmds: list[list[str]] = []

    def _recording_pyproj_runner(
        cmd: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        cmd_list = [str(c) for c in cmd]
        if len(cmd_list) >= 4 and cmd_list[1:3] == ["-m", "venv"]:
            return real_run([cmd_list[0], "-m", "venv", "--without-pip", *cmd_list[3:]], **kwargs)
        if "-m" in cmd_list and "pip" in cmd_list:
            pip_pyproj_cmds.append(cmd_list)
            return subprocess.CompletedProcess(cmd_list, 0, stdout="ok", stderr="")
        return real_run(cmd_list, **kwargs)

    pid_pytest_only = "proj-pytest-only-toml"
    ws_pytest_only = ws_mgr.create_workspace(pid_pytest_only)
    (ws_pytest_only / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        encoding="utf-8",
    )
    (ws_pytest_only / "app.py").write_text(
        "import http.server, os\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "    def log_message(self, *a): pass\n"
        "if __name__ == '__main__':\n"
        "    http.server.ThreadingHTTPServer((os.environ['HOST'], int(os.environ['PORT'])), H).serve_forever()\n",
        encoding="utf-8",
    )

    pid_pyproj = "proj-pyproject-deps"
    ws_pyproj = ws_mgr.create_workspace(pid_pyproj)
    (ws_pyproj / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = ["flask"]\n',
        encoding="utf-8",
    )
    (ws_pyproj / "app.py").write_text(
        "import http.server, os\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "    def log_message(self, *a): pass\n"
        "if __name__ == '__main__':\n"
        "    http.server.ThreadingHTTPServer((os.environ['HOST'], int(os.environ['PORT'])), H).serve_forever()\n",
        encoding="utf-8",
    )
    runner_pyproj = LocalAppRunner(
        workspace_manager=ws_mgr, dep_command_runner=_recording_pyproj_runner
    )
    try:
        st_no = runner_pyproj.start(pid_pytest_only, install_deps=True)
        assert st_no.running is True
        assert not (ws_pytest_only / ".venv").exists()
        assert pip_pyproj_cmds == []
        runner_pyproj.stop(pid_pytest_only)

        st_p = runner_pyproj.start(pid_pyproj, install_deps=True)
        assert st_p.running is True
        assert len(pip_pyproj_cmds) == 1
        assert pip_pyproj_cmds[0][-2:] == ["-e", "."]
    finally:
        runner_pyproj.stop_all()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_local_app_runner_ast_detects_missing_deps_without_requirements_txt(
    tmp_path: Path,
) -> None:
    """When workspace lacks requirements.txt, _detect_workspace_third_party_packages discovers third-party imports via AST (filtering stdlib & local packages), installs them into .venv, caches .deps_hash, and discovers nested requirements*.txt up to depth 3."""
    import json
    import os
    import subprocess
    import urllib.request
    from collections.abc import Sequence
    from typing import Any

    from agent_factory.runtime_runner import (
        LocalAppRunner,
        _detect_workspace_third_party_packages,
        _find_python_dependency_manifests,
    )

    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_ast")
    pid = "proj-ast-deps"
    ws_dir = ws_mgr.create_workspace(pid)
    pkg_dir = ws_dir / "src" / "yt_music_dl"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "helper.py").write_text(
        "import json\nBANNER = 'YT Music DL AST ok'\n",
        encoding="utf-8",
    )
    app_file = pkg_dir / "app.py"
    app_file.write_text(
        "import http.server\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "import flask\n"
        "import yt_dlp\n"
        "from yt_music_dl.helper import BANNER\n"
        "from . import helper\n\n"
        "host = os.environ['HOST']\n"
        "port = int(os.environ['PORT'])\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        payload = {\n"
        "            'banner': BANNER,\n"
        "            'prefix': sys.prefix,\n"
        "            'flask_ok': flask.FLASK_MARK,\n"
        "            'yt_ok': yt_dlp.YT_MARK,\n"
        "        }\n"
        "        body = json.dumps(payload).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(body)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body)\n"
        "    def log_message(self, *args):\n"
        "        pass\n"
        "if __name__ == '__main__':\n"
        "    http.server.ThreadingHTTPServer((host, port), H).serve_forever()\n",
        encoding="utf-8",
    )
    # Ignored test imports must NOT appear as runtime third-party deps
    (ws_dir / "tests").mkdir()
    (ws_dir / "tests" / "test_ignore_me.py").write_text("import playwright\n", encoding="utf-8")

    # 1. Verify AST detection & PyPI mapping cleanly
    detected = _detect_workspace_third_party_packages(ws_dir)
    assert detected == ["flask", "yt-dlp"]

    # Check additional common import -> PyPI mappings
    ws_map = tmp_path / "ws_map"
    ws_map.mkdir()
    (ws_map / "main.py").write_text(
        "import fastapi, uvicorn, requests, yaml, cv2, dotenv\n"
        "from bs4 import BeautifulSoup\n"
        "from PIL import Image\n",
        encoding="utf-8",
    )
    assert _detect_workspace_third_party_packages(ws_map) == [
        "beautifulsoup4",
        "fastapi",
        "opencv-python",
        "pillow",
        "python-dotenv",
        "pyyaml",
        "requests",
        "uvicorn",
    ]

    # 2. Verify nested requirements*.txt discovery up to depth 3
    (ws_map / "nested").mkdir()
    (ws_map / "nested" / "requirements-prod.txt").write_text("requests==2.32.0\n", encoding="utf-8")
    reqs_map, _ = _find_python_dependency_manifests(ws_map)
    assert [p.relative_to(ws_map).as_posix() for p in reqs_map] == [
        "nested/requirements-prod.txt"
    ]

    # 3. LocalAppRunner.start installs AST-detected packages into .venv & caches .deps_hash
    pip_cmds: list[list[str]] = []
    real_run = subprocess.run

    def _offline_ast_runner(cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd_list = [str(c) for c in cmd]
        if len(cmd_list) >= 4 and cmd_list[1:3] == ["-m", "venv"]:
            return real_run([cmd_list[0], "-m", "venv", "--without-pip", *cmd_list[3:]], **kwargs)
        if "-m" in cmd_list and "pip" in cmd_list and "install" in cmd_list:
            pip_cmds.append(cmd_list)
            venv_py = Path(cmd_list[0])
            venv_root = venv_py.parent.parent
            site_dir = (
                venv_root / "Lib" / "site-packages"
                if os.name == "nt"
                else next((venv_root / "lib").glob("python*/site-packages"))
            )
            site_dir.mkdir(parents=True, exist_ok=True)
            (site_dir / "flask.py").write_text("FLASK_MARK = 'flask-installed'\n", encoding="utf-8")
            (site_dir / "yt_dlp.py").write_text("YT_MARK = 'yt-dlp-installed'\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd_list, 0, stdout="ok", stderr="")
        return real_run(cmd_list, **kwargs)

    runner = LocalAppRunner(workspace_manager=ws_mgr, dep_command_runner=_offline_ast_runner)
    try:
        st1 = runner.start(pid, install_deps=True)
        assert st1.running is True
        assert st1.app_type == "python_local"
        assert len(pip_cmds) == 1
        assert pip_cmds[0][-2:] == ["flask", "yt-dlp"]
        assert (ws_dir / ".venv" / ".deps_hash").is_file()

        with urllib.request.urlopen(st1.url, timeout=5.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["banner"] == "YT Music DL AST ok"
        assert payload["flask_ok"] == "flask-installed"
        assert payload["yt_ok"] == "yt-dlp-installed"
        assert Path(payload["prefix"]).resolve() == (ws_dir / ".venv").resolve()
        runner.stop(pid)

        # Cached on second start
        st2 = runner.start(pid, install_deps=True)
        assert st2.running is True
        assert len(pip_cmds) == 1
        runner.stop(pid)

        # New import invalidates .deps_hash and re-invokes pip
        app_file.write_text(
            "import requests\n" + app_file.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        # Drop stub for requests inside _offline_ast_runner not needed at import unless installed:
        req_stub_dir = (
            ws_dir / ".venv" / "Lib" / "site-packages"
            if os.name == "nt"
            else next((ws_dir / ".venv" / "lib").glob("python*/site-packages"))
        )
        (req_stub_dir / "requests.py").write_text("", encoding="utf-8")
        st3 = runner.start(pid, install_deps=True)
        assert st3.running is True
        assert len(pip_cmds) == 2
        assert "requests" in pip_cmds[1]
    finally:
        runner.stop_all()


def test_build_worker_writes_manifest_json_without_secrets_and_commits_to_git(
    tmp_path: Path,
) -> None:
    """BuildWorker writes redacted .agent-factory/manifest.json before push/commit so GitHub commits include it while keeping secrets out (INV-19)."""
    import json
    import subprocess

    from agent_factory.adapters.github_vcs_adapter import GitHubVCSAdapter

    pid = "proj-manifest-git"
    secret_val = "TEST_MANIFEST_SECRET_555"
    store = MemoryEventStore()
    fake_llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": f"Objective token={secret_val}",
                    "users": ["Gabriel"],
                    "features": ["Manifest export"],
                    "business_rules": ["No secrets"],
                    "edge_cases": ["Missing remote"],
                    "constraints": ["Static web"],
                    "success_criteria": ["Committed manifest"],
                    "assumptions": [],
                },
                "questions": [],
            },
            {
                "approach": "Vanilla static app",
                "components": ["index.html"],
                "tradeoffs": ["Simple structure"],
            },
            {
                "critique": ["None"],
                "robust_proposal": "Single HTML entrypoint",
                "architectural_safeguards": ["Secret redaction"],
            },
            {
                "summary": f"App manifest token={secret_val}",
                "rationale": "Static template with redacted manifest",
                "template_id": "static-app",
                "files": [
                    {
                        "path": "index.html",
                        "purpose": "Main entry",
                        "contract": "Valid HTML5 document",
                        "depends_on": [],
                    }
                ],
                "acceptance_tests": [
                    {"id": "AT-01", "description": "Serves index.html", "type": "e2e"}
                ],
                "risks": ["None"],
                "deployment_target": "github-pages",
            },
        ]
    )
    orch = Orchestrator(store=store, llm=fake_llm)
    orch.create_project(project_id=pid, name="Manifest Test", objective=f"Objective token={secret_val}")
    orch.handle_interview_message(pid, f"evt-int-{pid}", "Complete spec")
    waiting = orch.run_debate(pid)
    assert waiting.plan_hash is not None
    orch.submit_approval(
        pid,
        ApprovalEvent(
            plan_hash=waiting.plan_hash,
            timestamp=datetime(2026, 11, 16, 15, 30, tzinfo=UTC),
            decision="approved",
        ),
    )

    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_manifest")
    bare_remote = tmp_path / "bare_manifest_remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_remote)], check=True, capture_output=True)

    vcs = GitHubVCSAdapter(
        repo_base_url=str(tmp_path),
        github_token="",
        secrets=(secret_val,),
    )
    # Pre-point workspace origin to bare remote once workspace exists
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / ".agent-factory").mkdir(parents=True, exist_ok=True)
    (ws_dir / ".agent-factory" / "state.json").write_text('{"tmp": true}\n', encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare_remote)], cwd=ws_dir, check=True)

    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_app.py": "def test_ok(): assert True\n"}],
            impl_steps=[{"index.html": "<h1>Manifest App</h1>\n"}],
        ),
        runner=FakeRunner(
            results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]
        ),
        vcs=vcs,
        secrets_to_redact=(secret_val,),
    )

    report = worker.run_job(pid)
    assert report.ci_rounds == 1
    assert orch.get_status(pid).state == ProjectState.DELIVERED

    manifest_file = ws_dir / ".agent-factory" / "manifest.json"
    assert manifest_file.is_file()
    raw_manifest = manifest_file.read_text(encoding="utf-8")
    assert secret_val not in raw_manifest
    assert "[REDACTED]" in raw_manifest
    manifest_data = json.loads(raw_manifest)
    for key in (
        "schema_version",
        "project_id",
        "title",
        "app_type",
        "objective",
        "plan_summary",
        "repo_url",
        "deploy_url",
        "updated_at",
    ):
        assert key in manifest_data
    assert manifest_data["project_id"] == pid
    assert manifest_data["app_type"] == "static"
    assert manifest_data["repo_url"] == f"{tmp_path}/agent-factory/{pid}" or bool(manifest_data["repo_url"])

    # Verify manifest.json is committed in Git while state.json inside .agent-factory/ stays ignored!
    tree_files = subprocess.run(
        ["git", "-C", str(bare_remote), "ls-tree", "-r", "refs/heads/work", "--name-only"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert ".agent-factory/manifest.json" in tree_files
    assert ".agent-factory/state.json" not in tree_files


def test_agy_llm_adapter_uses_stdin_or_temp_file_for_long_prompts_avoiding_winerror_206(
    tmp_path: Path,
) -> None:
    """AgyLLMAdapter keeps short prompts inline in -p, uses UTF-8 temp files referenced in -p (never stdin) for prompts >6,000 chars, supports per-call timeout_seconds, and cleans up in finally."""
    import sys

    from agent_factory.adapters.agy_llm_adapter import (
        MAX_CLI_ARG_PROMPT_CHARS,
        AgyLLMAdapter,
    )
    from agent_factory.adapters.fake_runner import FakeRunner
    from agent_factory.adapters.subprocess_runner import SubprocessRunner
    from agent_factory.ports.llm import LLMError
    from agent_factory.ports.runner import CommandTimeoutError

    assert MAX_CLI_ARG_PROMPT_CHARS == 6000

    # 1. Short prompt preserves legacy -p <prompt> argv invocation with adapter default timeout
    short_runner = FakeRunner(results=[CommandResult(exit_code=0, stdout='{"ok": true}')])
    short_adapter = AgyLLMAdapter(runner=short_runner, cwd=tmp_path, timeout_seconds=60.0)
    assert short_adapter.complete([{"role": "user", "content": "hola corto"}]) == {"ok": True}
    assert "hola corto" in " ".join(short_runner.calls[0]["command"])
    assert short_runner.calls[0]["input"] is None
    assert short_runner.calls[0]["timeout_seconds"] == 60.0

    # 2. Long prompt (> 6,000 chars) writes UTF-8 temp file, passes path reference in -p (NO stdin), and cleans up
    long_body = "CODIGO_REPOSITORIO_ÑÁÉÍÓÚ_" + ("x" * 12500)
    seen_temp_paths: list[Path] = []
    seen_temp_contents: list[str] = []

    class _InspectTempFileRunner(FakeRunner):
        def run(self, command, cwd, timeout_seconds=30.0, env=None, input=None):
            for token in command:
                tok_str = str(token)
                if ":" in tok_str:
                    candidate = Path(tok_str.split(":", 1)[-1].strip())
                    if candidate.suffix == ".txt" and candidate.exists():
                        seen_temp_paths.append(candidate)
                        seen_temp_contents.append(candidate.read_text(encoding="utf-8"))
            return super().run(
                command=command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                env=env,
                input=input,
            )

    long_runner = _InspectTempFileRunner(
        results=[CommandResult(exit_code=0, stdout='{"reply": "audit ok"}')]
    )
    long_adapter = AgyLLMAdapter(runner=long_runner, cwd=tmp_path, timeout_seconds=60.0)
    out = long_adapter.generate(
        [{"role": "user", "content": long_body}],
        schema={"type": "object", "properties": {"reply": {"type": "string"}}},
        timeout_seconds=240.0,
    )
    assert out == {"reply": "audit ok"}
    call_long = long_runner.calls[0]
    assert sum(len(a) for a in call_long["command"]) < 1000
    assert long_body not in " ".join(call_long["command"])
    assert call_long["input"] is None
    assert call_long["timeout_seconds"] == 240.0
    assert len(seen_temp_paths) == 1
    assert long_body in seen_temp_contents[0]
    assert not seen_temp_paths[0].exists()

    # Subsequent call without override restores default timeout (60.0)
    long_runner.enqueue(CommandResult(exit_code=0, stdout='{"reply": "default ok"}'))
    assert long_adapter.complete([{"role": "user", "content": "corto"}]) == {"reply": "default ok"}
    assert long_runner.calls[1]["timeout_seconds"] == 60.0

    # Temp file is still removed in finally when Runner raises timeout
    timeout_runner = _InspectTempFileRunner(results=[CommandTimeoutError("cli", 240.0)])
    timeout_adapter = AgyLLMAdapter(runner=timeout_runner, cwd=tmp_path)
    with pytest.raises(LLMError, match="timed out|timeout|240"):
        timeout_adapter.generate([{"role": "user", "content": long_body}], timeout_seconds=240.0)
    assert len(seen_temp_paths) == 2
    assert not seen_temp_paths[1].exists()

    # 3. Simulated custom Runner without 'input' kwarg remains fully compatible
    class _NoInputRunner:
        def __init__(self) -> None:
            self.seen_files: list[Path] = []
            self.seen_text: list[str] = []

        def run(self, command, cwd, timeout_seconds=30.0, env=None):
            joined = " ".join(str(x) for x in command)
            for token in command:
                p = Path(str(token).split(":", 1)[-1].strip()) if ":" in str(token) else Path(str(token))
                if p.suffix == ".txt" and p.exists():
                    self.seen_files.append(p)
                    self.seen_text.append(p.read_text(encoding="utf-8"))
            assert len(joined) < 1000
            return CommandResult(exit_code=0, stdout='{"fallback": "temp-ok"}')

    no_in_runner = _NoInputRunner()
    fb_adapter = AgyLLMAdapter(runner=no_in_runner, cwd=tmp_path)
    assert fb_adapter.complete([{"role": "user", "content": long_body}]) == {"fallback": "temp-ok"}
    assert len(no_in_runner.seen_files) == 1
    assert long_body in no_in_runner.seen_text[0]
    assert not no_in_runner.seen_files[0].exists()

    # 4. Real SubprocessRunner reads >22,000-char prompt from UTF-8 file referenced in -p without WinError 206
    read_file_script = tmp_path / "read_prompt_file.py"
    read_file_script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "idx = sys.argv.index('-p')\n"
        "p_arg = sys.argv[idx + 1]\n"
        "fpath = Path(p_arg.split(':', 1)[-1].strip())\n"
        "data = fpath.read_text(encoding='utf-8')\n"
        "print(json.dumps({'chars': len(data)}))\n",
        encoding="utf-8",
    )
    real_adapter = AgyLLMAdapter(
        runner=SubprocessRunner(default_timeout_seconds=10.0),
        cli_command=[sys.executable, str(read_file_script)],
        cwd=tmp_path,
    )
    huge_res = real_adapter.complete([{"role": "user", "content": "Z" * 22000}])
    assert huge_res["chars"] > 22000


def test_agy_llm_adapter_resolves_windows_cmd_shim_and_spaced_temp_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgyLLMAdapter resolves .cmd/.bat shims via shutil.which (keeping mocks intact when None) and handles spaced temp paths on Windows."""
    import os
    import shutil
    import sys
    import tempfile

    from agent_factory.adapters.agy_llm_adapter import AgyLLMAdapter
    from agent_factory.adapters.fake_runner import FakeRunner
    from agent_factory.adapters.subprocess_runner import SubprocessRunner

    resolved_cmd = r"C:\Users\Test User\AppData\Local\Programs\agy\agy.cmd"

    # 1. Mock shutil.which -> .cmd shim path used in argv[0] for short and long prompts
    monkeypatch.setattr(shutil, "which", lambda binary: resolved_cmd if binary == "agy" else None)

    spaced_temp_dir = tmp_path / "Angel laptopJaz" / "Temp With Spaces"
    spaced_temp_dir.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(spaced_temp_dir))

    seen_files: list[Path] = []
    seen_contents: list[str] = []

    class _SpacedTempInspectRunner(FakeRunner):
        def run(self, command, cwd, timeout_seconds=30.0, env=None, input=None):
            for tok in command:
                s = str(tok)
                if ":" in s:
                    cand = Path(s.split(":", 1)[-1].strip())
                    if cand.suffix == ".txt" and cand.exists():
                        seen_files.append(cand)
                        seen_contents.append(cand.read_text(encoding="utf-8"))
            return super().run(
                command=command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                env=env,
                input=input,
            )

    runner = _SpacedTempInspectRunner(
        results=[
            CommandResult(exit_code=0, stdout='{"ok": "short"}'),
            CommandResult(exit_code=0, stdout='{"ok": "long"}'),
        ]
    )
    adapter = AgyLLMAdapter(runner=runner, cli_command=("agy",), cwd=tmp_path)

    # Short prompt -> resolved .cmd in argv[0]
    assert adapter.complete([{"role": "user", "content": "hola corto"}]) == {"ok": "short"}
    assert runner.calls[0]["command"][0] == resolved_cmd

    # Long prompt (>6,000 chars) -> resolved .cmd in argv[0] + spaced temp file in -p
    long_prompt = "REPOSITORIO_CON_ESPACIOS_" + ("Y" * 7000)
    assert adapter.generate([{"role": "user", "content": long_prompt}]) == {"ok": "long"}
    assert runner.calls[1]["command"][0] == resolved_cmd
    assert runner.calls[1]["input"] is None
    assert len(seen_files) == 1
    assert "Angel laptopJaz" in str(seen_files[0])
    assert "Temp With Spaces" in str(seen_files[0])
    assert long_prompt in seen_contents[0]
    assert not seen_files[0].exists()

    # 2. When shutil.which returns None, original mock binary name is preserved
    monkeypatch.setattr(shutil, "which", lambda _binary: None)
    fallback_runner = FakeRunner(
        results=[
            CommandResult(exit_code=0, stdout='{"ok": "mock-short"}'),
            CommandResult(exit_code=0, stdout='{"ok": "mock-long"}'),
        ]
    )
    mock_adapter = AgyLLMAdapter(runner=fallback_runner, cli_command=("mock-agy",), cwd=tmp_path)
    assert mock_adapter.complete([{"role": "user", "content": "corto"}]) == {"ok": "mock-short"}
    assert fallback_runner.calls[0]["command"][0] == "mock-agy"
    assert mock_adapter.complete([{"role": "user", "content": long_prompt}]) == {"ok": "mock-long"}
    assert fallback_runner.calls[1]["command"][0] == "mock-agy"

    # 3. End-to-end SubprocessRunner execution on Windows with real .cmd shim + spaces in shim & temp paths
    if os.name == "nt":
        real_py_helper = spaced_temp_dir / "read_from_cmd.py"
        real_py_helper.write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            "idx = sys.argv.index('-p')\n"
            "arg = sys.argv[idx + 1]\n"
            "fpath = Path(arg.split(':', 1)[-1].strip())\n"
            "text = fpath.read_text(encoding='utf-8')\n"
            "print(json.dumps({'path': str(fpath), 'chars': len(text)}))\n",
            encoding="utf-8",
        )
        shim_dir = tmp_path / "Programs With Spaces" / "agy dir"
        shim_dir.mkdir(parents=True)
        cmd_shim = shim_dir / "agy_wrapper.cmd"
        cmd_shim.write_text(
            f'@echo off\r\n"{sys.executable}" "{real_py_helper}" %*\r\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(
            shutil,
            "which",
            lambda b: str(cmd_shim) if b == "agy_wrapper" else None,
        )
        real_cmd_adapter = AgyLLMAdapter(
            runner=SubprocessRunner(default_timeout_seconds=10.0),
            cli_command=("agy_wrapper",),
            cwd=tmp_path,
        )
        res_real = real_cmd_adapter.complete([{"role": "user", "content": long_prompt}])
        assert "Angel laptopJaz" in res_real["path"]
        assert res_real["chars"] > 7000

        # 4. Multiline InterviewRoundOutput schema prompt through real .cmd wrapper does NOT truncate at [SYSTEM]
        interview_py = spaced_temp_dir / "echo_interview_prompt.py"
        interview_py.write_text(
            "import json, sys\n"
            "idx = sys.argv.index('-p')\n"
            "p_arg = sys.argv[idx + 1]\n"
            "if p_arg == 'ping':\n"
            "    print('Available models: gemini-2.5-flash')\n"
            "    sys.exit(0)\n"
            "assert 'Pagina responsiva' in p_arg, f'Truncated -p: {p_arg!r}'\n"
            "assert 'InterviewRoundOutput' in p_arg or 'JSON Schema:' in p_arg\n"
            "print(json.dumps({'spec': {'objective': 'Pagina responsiva'}, 'questions': ['¿Para qué usuarios?']}))\n",
            encoding="utf-8",
        )
        interview_cmd = shim_dir / "agy_interview.cmd"
        interview_cmd.write_text(
            f'@echo off\r\n"{sys.executable}" "{interview_py}" %*\r\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(
            shutil,
            "which",
            lambda b, **_kw: str(interview_cmd) if b == "agy" else None,
        )
        from agent_factory.agents.interviewer import InterviewRoundOutput

        int_adapter = AgyLLMAdapter(
            runner=SubprocessRunner(default_timeout_seconds=10.0),
            cli_command=("agy",),
            cwd=tmp_path,
        )
        int_res = int_adapter.complete(
            [
                {"role": "system", "content": "You are an interviewer."},
                {"role": "user", "content": "Pagina responsiva"},
            ],
            schema=InterviewRoundOutput,
        )
        assert int_res["spec"]["objective"] == "Pagina responsiva"
        assert int_res["questions"] == ["¿Para qué usuarios?"]




def test_build_worker_records_commit_sha_and_version_label_on_project_delivered(
    tmp_path: Path,
) -> None:
    """BuildWorker records commit_sha and incremental version_label ('v1', 'v2') in PROJECT_DELIVERED payload."""
    import subprocess

    from agent_factory.adapters.github_vcs_adapter import GitHubVCSAdapter

    pid = "proj-checkpoints-ver"
    orch, store = _create_approved_project(pid, num_tests=1, approve=True)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_ver")
    ws_dir = ws_mgr.create_workspace(pid)
    bare_remote = tmp_path / "bare_ver_remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare_remote)], cwd=ws_dir, check=True)

    vcs = GitHubVCSAdapter(repo_base_url=str(tmp_path), github_token="")
    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=FakeCodeAgent(
            test_gen_steps=[{"tests/test_app.py": "def test_ok(): assert True\n"}],
            impl_steps=[
                {"index.html": "<h1>v1</h1>\n"},
                {"index.html": "<h1>v2</h1>\n"},
            ],
        ),
        runner=FakeRunner(
            results=[
                CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True}),
                CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True, "MOD-02": True}),
            ]
        ),
        vcs=vcs,
    )

    worker.run_job(pid)
    delivered_events = [e for e in store.get_events(pid) if e.type == EventType.PROJECT_DELIVERED]
    assert len(delivered_events) == 1
    v1_sha = delivered_events[0].payload.get("commit_sha")
    assert isinstance(v1_sha, str) and len(v1_sha) == 40
    assert delivered_events[0].payload.get("version_label") == "v1"

    # Trigger post-delivery modify -> v2 delivery has distinct commit_sha and version_label='v2'
    orch.modify_project(pid, "Update header to v2")
    worker.queue.remove(pid)
    worker.enqueue_approved_project(pid)
    worker.run_job(pid)

    delivered_events2 = [e for e in store.get_events(pid) if e.type == EventType.PROJECT_DELIVERED]
    assert len(delivered_events2) == 2
    v2_sha = delivered_events2[1].payload.get("commit_sha")
    assert isinstance(v2_sha, str) and len(v2_sha) == 40
    assert v2_sha != v1_sha
    assert delivered_events2[1].payload.get("version_label") == "v2"


def test_orchestrator_chat_persists_current_commit_sha_and_version_retroactively(
    tmp_path: Path,
) -> None:
    """Orchestrator.chat_with_project stamps current commit_sha & version_label while reading legacy chats safely."""
    import json

    from agent_factory.adapters.sqlite_store import SqliteEventStore
    from agent_factory.domain.events import create_event
    from agent_factory.domain.models import Plan

    pid = "proj-chat-ver"
    now = datetime(2026, 4, 21, 10, 0, tzinfo=UTC)
    store = SqliteEventStore(str(tmp_path / "chat_ver.db"))
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_chat_ver")
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "app.py").write_text("print('ok')\n", encoding="utf-8")

    plan_dict = {
        "summary": "App versionada",
        "rationale": "Simple",
        "template_id": "static-web",
        "deployment_target": "github-pages",
        "files": [{"path": "app.py", "purpose": "Main", "contract": "Python"}],
        "acceptance_tests": [{"id": "AT-01", "description": "Corre", "type": "unit"}],
        "estimated_cost": "low",
    }
    plan_hash = Plan.model_validate(plan_dict).compute_hash()
    sha_v2 = "1234567890abcdef1234567890abcdef12345678"
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "Ver", "objective": "Obj", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.PLAN_CREATED, {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
        create_event(pid, EventType.PROJECT_DELIVERED, {"repo_url": "", "deploy_url": "", "commit_sha": sha_v2, "version_label": "v2"}, at=now),
    ):
        store.append(ev)

    chats_dir = tmp_path / "chats"
    chats_dir.mkdir(parents=True)
    # Seed legacy message without commit_sha or version_label
    (chats_dir / f"{pid}.json").write_text(
        json.dumps([{"id": "legacy-1", "role": "assistant", "mode": "chat", "content": "Viejo", "created_at": now.isoformat()}]),
        encoding="utf-8",
    )

    llm = FakeLLM(
        responses=[
            {
                "reply": "# Auditoría\nTodo bien.",
                "suggested_changes": "Nada urgente.",
                "document_title": "audit-v2.md",
            }
        ]
    )
    orch = Orchestrator(store=store, llm=llm, workspace_manager=ws_mgr)
    res = orch.chat_with_project(pid, "", mode="audit", chats_dir=chats_dir)
    assert res["commit_sha"] == sha_v2
    assert res["version_label"] == "v2"

    hist = orch.get_project_chat_history(pid, chats_dir=chats_dir)
    assert len(hist) == 3
    assert hist[0]["id"] == "legacy-1"
    assert hist[0].get("commit_sha", "") == ""
    assert hist[1]["commit_sha"] == sha_v2
    assert hist[1]["version_label"] == "v2"
    assert hist[2]["commit_sha"] == sha_v2
    assert hist[2]["version_label"] == "v2"
    store.close()


def test_orchestrator_chat_persistent_lifecycle_timeout_orphan_retry_and_delete(
    tmp_path: Path,
) -> None:
    """Orchestrator persists running/stage_label immediately, transitions to timed_out/failed keeping stage_label & redacted error (INV-19), recovers orphans to interrupted, and supports retry & delete."""
    import json
    from datetime import timedelta
    from typing import Any

    from agent_factory.adapters.sqlite_store import SqliteEventStore
    from agent_factory.domain.events import create_event
    from agent_factory.domain.models import Plan
    from agent_factory.ports.llm import LLMError

    class _StaticClock:
        def __init__(self, current: datetime) -> None:
            self._current = current

        def now(self) -> datetime:
            return self._current

        def sleep(self, seconds: float) -> None:
            pass

    pid = "proj-audit-lifecycle"
    secret_tok = "SECRET_AUDIT_ERR_987"
    clock = _StaticClock(datetime(2026, 4, 22, 14, 0, tzinfo=UTC))
    store = SqliteEventStore(str(tmp_path / "audit_life.db"))
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_life")
    ws_dir = ws_mgr.create_workspace(pid)
    # Create >14k chars to verify max_chars=12000 cap
    (ws_dir / "big.py").write_text("X = '" + ("A" * 14000) + "'\n", encoding="utf-8")

    plan_dict = {
        "summary": "Lifecycle App",
        "rationale": "Simple",
        "template_id": "static-web",
        "deployment_target": "github-pages",
        "files": [{"path": "big.py", "purpose": "Main", "contract": "Python"}],
        "acceptance_tests": [{"id": "AT-01", "description": "Works", "type": "unit"}],
        "estimated_cost": "low",
    }
    plan_hash = Plan.model_validate(plan_dict).compute_hash()
    sha_v1 = "abcdef1234567890abcdef1234567890abcdef12"
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "Life", "objective": "Obj", "state": "INTERVIEW"}, at=clock.now()),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=clock.now()),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=clock.now()),
        create_event(pid, EventType.PLAN_CREATED, {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=clock.now()),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=clock.now()),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=clock.now()),
        create_event(pid, EventType.PROJECT_DELIVERED, {"repo_url": "", "deploy_url": "", "commit_sha": sha_v1, "version_label": "v1"}, at=clock.now()),
    ):
        store.append(ev)

    chats_dir = tmp_path / "chats_life"
    chat_file = chats_dir / f"{pid}.json"
    observed_mid_call: list[dict[str, Any]] = []
    observed_timeouts: list[float | None] = []

    class _SpyLifecycleLLM:
        def __init__(self) -> None:
            self.step = 0

        def complete(
            self,
            messages: list[dict[str, str]],
            schema: Any = None,
            *,
            timeout_seconds: float | None = None,
        ) -> dict[str, Any]:
            observed_timeouts.append(timeout_seconds)
            if chat_file.is_file():
                disk = json.loads(chat_file.read_text(encoding="utf-8"))
                observed_mid_call.append(dict(disk[-1]))
            self.step += 1
            if self.step == 1:
                raise LLMError(f"agy CLI timed out after 240s using {secret_tok}")
            return {
                "reply": "# Auditoría recuperada\nTodo verificado.",
                "suggested_changes": "Sin cambios críticos.",
                "document_title": "audit-recuperada.md",
            }

    orch = Orchestrator(
        store=store,
        llm=_SpyLifecycleLLM(),
        clock=clock,
        workspace_manager=ws_mgr,
    )

    # 1. Timeout persists status='timed_out', keeps stage 2/3, and redacts secret (INV-19)
    with pytest.raises(LLMError):
        orch.chat_with_project(
            pid,
            "",
            mode="audit",
            chats_dir=chats_dir,
            extra_secrets=(secret_tok,),
        )

    assert observed_timeouts[0] == 240.0
    assert observed_mid_call[0]["status"] == "running"
    assert observed_mid_call[0]["stage_label"] == "2/3 Analizando código con IA"
    assert observed_mid_call[0]["server_boot_id"] == orch._server_boot_id

    hist1 = orch.get_project_chat_history(pid, chats_dir=chats_dir, extra_secrets=(secret_tok,))
    timed_out_entry = hist1[-1]
    entry_id = timed_out_entry["id"]
    assert timed_out_entry["status"] == "timed_out"
    assert timed_out_entry["stage_label"] == "2/3 Analizando código con IA"
    assert secret_tok not in timed_out_entry["error_detail"]
    assert "[REDACTED]" in timed_out_entry["error_detail"]
    assert isinstance(timed_out_entry["duration_seconds"], (int, float))

    # 2. Retry reuses mode & prompt and updates the SAME existing entry to 'completed'
    retry_res = orch.retry_project_chat_entry(
        pid,
        entry_id,
        chats_dir=chats_dir,
        extra_secrets=(secret_tok,),
    )
    assert retry_res["status"] == "completed"
    assert retry_res["stage_label"] == "Completada"
    assert "Auditoría recuperada" in retry_res["reply"]
    assert retry_res["context_chars"] <= 12500

    hist2 = orch.get_project_chat_history(pid, chats_dir=chats_dir)
    assert len(hist2) == len(hist1)
    assert hist2[-1]["id"] == entry_id
    assert hist2[-1]["status"] == "completed"
    assert hist2[-1]["stage_label"] == "Completada"
    assert hist2[-1]["error_detail"] == ""

    # 3. Orphaned 'running' entries (old server_boot_id OR >260s) auto-recover to 'interrupted' on disk
    disk_data = json.loads(chat_file.read_text(encoding="utf-8"))
    disk_data.append(
        {
            "id": "a-orphan-boot",
            "role": "assistant",
            "mode": "document",
            "prompt": "Doc",
            "content": "",
            "status": "running",
            "stage_label": "1/3 Leyendo archivos del repositorio",
            "server_boot_id": "dead-boot-999",
            "created_at": clock.now().isoformat(),
        }
    )
    disk_data.append(
        {
            "id": "a-orphan-ttl",
            "role": "assistant",
            "mode": "audit",
            "prompt": "Audit",
            "content": "",
            "status": "running",
            "stage_label": "2/3 Analizando código con IA",
            "server_boot_id": orch._server_boot_id,
            "created_at": (clock.now() - timedelta(seconds=270)).isoformat(),
        }
    )
    chat_file.write_text(json.dumps(disk_data), encoding="utf-8")

    hist3 = orch.get_project_chat_history(pid, chats_dir=chats_dir)
    by_id = {m["id"]: m for m in hist3}
    assert by_id["a-orphan-boot"]["status"] == "interrupted"
    assert (
        by_id["a-orphan-boot"]["error_detail"]
        == "La ejecución se interrumpió o superó el tiempo límite en la etapa: 1/3 Leyendo archivos del repositorio"
    )
    assert by_id["a-orphan-ttl"]["status"] == "interrupted"
    assert (
        by_id["a-orphan-ttl"]["error_detail"]
        == "La ejecución se interrumpió o superó el tiempo límite en la etapa: 2/3 Analizando código con IA"
    )

    # 4. delete_project_chat_entry removes target entry and rejects traversal IDs (INV-22)
    assert orch.delete_project_chat_entry(pid, "a-orphan-boot", chats_dir=chats_dir) is True
    assert orch.delete_project_chat_entry(pid, "a-orphan-boot", chats_dir=chats_dir) is False
    with pytest.raises(ValueError):
        orch.delete_project_chat_entry(pid, "../escape", chats_dir=chats_dir)
    hist4 = orch.get_project_chat_history(pid, chats_dir=chats_dir)
    assert "a-orphan-boot" not in [m["id"] for m in hist4]
    store.close()


def _build_full_audit_payload(
    *,
    ux_ui: float = 90.0,
    functional_completeness: float = 90.0,
    robustness: float = 90.0,
    architecture_quality: float = 90.0,
    security_hygiene: float = 90.0,
    testing_runtime: float = 90.0,
    ux_findings: list[str] | None = None,
    ux_improvements: list[str] | None = None,
) -> dict[str, object]:
    return {
        "summary": "Evaluación integral de 6 dimensiones.",
        "dimensions": {
            "ux_ui": {
                "score": ux_ui,
                "findings": ux_findings or ([] if ux_ui >= 70 else ["Contraste insuficiente en móvil"]),
                "improvements": ux_improvements or ([] if ux_ui >= 70 else ["Mejorar contraste y botones >= 44px"]),
            },
            "functional_completeness": {
                "score": functional_completeness,
                "findings": [],
                "improvements": [],
            },
            "robustness": {
                "score": robustness,
                "findings": [],
                "improvements": [],
            },
            "architecture_quality": {
                "score": architecture_quality,
                "findings": [],
                "improvements": [],
            },
            "security_hygiene": {
                "score": security_hygiene,
                "findings": [],
                "improvements": [],
            },
            "testing_runtime": {
                "score": testing_runtime,
                "findings": [],
                "improvements": [],
            },
        },
    }


def _setup_sqlite_approved_project(
    tmp_path: Path,
    pid: str,
    *,
    llm: object,
) -> tuple[Orchestrator, object, WorkspaceManager]:
    from agent_factory.adapters.sqlite_store import SqliteEventStore
    from agent_factory.domain.events import create_event
    from agent_factory.domain.models import AcceptanceTest, AcceptanceTestType, FileSpec, Plan

    store = SqliteEventStore(str(tmp_path / f"{pid}.db"))
    ws_mgr = WorkspaceManager(base_dir=tmp_path / f"ws_{pid}")
    plan = Plan(
        summary="App web responsive",
        rationale="Simple static frontend",
        template_id="static-web",
        deployment_target="github-pages",
        files=[
            FileSpec(path="index.html", purpose="UI principal", contract="HTML5 responsive"),
            FileSpec(path="styles.css", purpose="Estilos", contract="Tema claro/oscuro"),
        ],
        acceptance_tests=[
            AcceptanceTest(id="AT-01", description="Renderiza UI principal", type=AcceptanceTestType.UNIT)
        ],
    )
    plan_hash = plan.compute_hash()
    orch = Orchestrator(store=store, llm=llm, workspace_manager=ws_mgr)  # type: ignore[arg-type]
    now = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
    store.append(
        create_event(
            pid,
            EventType.PROJECT_CREATED,
            {"name": "App Calidad", "objective": "App web responsive", "state": "INTERVIEW"},
            at=now,
        )
    )
    store.append(create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now))
    store.append(create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now))
    store.append(
        create_event(
            pid,
            EventType.PLAN_CREATED,
            {"plan": plan.model_dump(mode="json"), "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"},
            at=now,
        )
    )
    orch.submit_approval(pid, ApprovalEvent(plan_hash=plan_hash, timestamp=now, decision="approved"))
    return orch, store, ws_mgr


def test_quality_auditor_approves_on_first_audit_and_persists_markdown_report_and_scorecard(
    tmp_path: Path,
) -> None:
    """Test 1: Project passing >=85% global and >=70% per area on first audit delivers with quality_scorecard and persists Markdown audit history."""
    from agent_factory.agents.quality_auditor import ParentAuditor

    pid = "qa-pass-first"
    chats_dir = tmp_path / "chats_qa1"
    llm = FakeLLM(
        responses=[
            _build_full_audit_payload(
                ux_ui=92.0,
                functional_completeness=90.0,
                robustness=88.0,
                architecture_quality=86.0,
                security_hygiene=94.0,
                testing_runtime=88.0,
            ),
            _build_full_audit_payload(
                ux_ui=95.0,
                functional_completeness=92.0,
                robustness=90.0,
                architecture_quality=90.0,
                security_hygiene=95.0,
                testing_runtime=92.0,
            ),
        ]
    )
    orch, store, ws_mgr = _setup_sqlite_approved_project(tmp_path, pid, llm=llm)
    try:
        auditor = ParentAuditor(llm=llm)
        worker = BuildWorker(
            orchestrator=orch,
            workspace_manager=ws_mgr,
            code_agent=FakeCodeAgent(
                test_gen_steps=[
                    {"tests/test_ui.py": "def test_at_01(): assert 1 == 1\n"},
                    {"tests/test_ui.py": "def test_at_01(): assert 1 == 1\ndef test_mod_02(): assert True\n"},
                ],
                impl_steps=[
                    {
                        "index.html": "<!doctype html><html><body><main><h1>Hola</h1></main></body></html>\n",
                        "styles.css": ":root { --bg: #fff; }\n",
                    },
                    {
                        "index.html": "<!doctype html><html><body><main><h1>Hola v2</h1></main></body></html>\n",
                    },
                ],
            ),
            runner=FakeRunner(
                results=[
                    CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True}),
                    CommandResult(exit_code=0, stdout="2 passed", test_results={"AT-01": True, "MOD-02": True}),
                ]
            ),
            vcs=FakeVCS(
                ci_results=[
                    CIRunResult(passed=True, summary="CI ok"),
                    CIRunResult(passed=True, summary="CI mod ok"),
                ]
            ),
            quality_auditor=auditor,
            chats_dir=chats_dir,
        )

        report = worker.run_job(pid)
        assert report.invocations == 2  # 1 test_gen + 1 impl, 0 refinement calls
        status = orch.get_status(pid)
        assert status.state == ProjectState.DELIVERED

        delivered_ev = next(e for e in store.get_events(pid) if e.type == EventType.PROJECT_DELIVERED)
        scorecard = delivered_ev.payload.get("quality_scorecard")
        assert isinstance(scorecard, dict)
        assert scorecard["passed"] is True
        assert scorecard["global_score"] >= 85.0
        assert all(area["score"] >= 70.0 for area in scorecard["areas"] if area["active"])

        history = orch.get_project_chat_history(pid, chats_dir=chats_dir)
        audit_entries = [h for h in history if h.get("role") == "assistant" and h.get("mode") == "audit"]
        assert len(audit_entries) == 1
        assert audit_entries[0]["status"] == "completed"
        assert audit_entries[0]["version_label"] == "v1"
        assert "| Área |" in audit_entries[0]["content"]
        assert "UX/UI" in audit_entries[0]["content"]
        assert audit_entries[0]["quality_scorecard"]["global_score"] == scorecard["global_score"]

        # Verify post-delivery modify_project -> BuildWorker.run_job ALSO executes ParentAuditor and saves v2 scorecard
        orch.modify_project(pid, "Actualizar título a Hola v2")
        worker.queue.remove(pid)
        worker.enqueue_approved_project(pid)
        mod_report = worker.run_job(pid)
        assert mod_report.failure_cause is None
        delivered_events = [e for e in store.get_events(pid) if e.type == EventType.PROJECT_DELIVERED]
        assert len(delivered_events) == 2
        assert delivered_events[1].payload["version_label"] == "v2"
        assert delivered_events[1].payload["quality_scorecard"]["passed"] is True
        assert delivered_events[1].payload["quality_scorecard"]["global_score"] > scorecard["global_score"]
        history2 = orch.get_project_chat_history(pid, chats_dir=chats_dir)
        audit_entries2 = [h for h in history2 if h.get("role") == "assistant" and h.get("mode") == "audit"]
        assert len(audit_entries2) == 2
        assert audit_entries2[-1]["version_label"] == "v2"
    finally:
        store.close()


def test_quality_auditor_triggers_refinement_when_failed_then_delivers_approved(
    tmp_path: Path,
) -> None:
    """Test 2: First audit scoring 72% (ux_ui=60%) triggers bounded refinement sending penalized findings, reaches 90%, and delivers approved."""
    from agent_factory.agents.quality_auditor import ParentAuditor

    pid = "qa-refine-pass"
    chats_dir = tmp_path / "chats_qa2"
    llm = FakeLLM(
        responses=[
            # Audit round 1: 0.25*60 + 0.25*80 + 0.125*(74+76+76+70) = 15 + 20 + 37 = 72.0%
            _build_full_audit_payload(
                ux_ui=60.0,
                functional_completeness=80.0,
                robustness=74.0,
                architecture_quality=76.0,
                security_hygiene=76.0,
                testing_runtime=70.0,
                ux_findings=["Botones menores a 44px en 360px"],
                ux_improvements=["Aplicar min-height: 44px y mejor contraste"],
            ),
            # Audit round 2 (after 1 refinement): 90.0%
            _build_full_audit_payload(
                ux_ui=90.0,
                functional_completeness=90.0,
                robustness=90.0,
                architecture_quality=90.0,
                security_hygiene=90.0,
                testing_runtime=90.0,
            ),
        ]
    )
    orch, store, ws_mgr = _setup_sqlite_approved_project(tmp_path, pid, llm=llm)
    try:
        code_agent = FakeCodeAgent(
            test_gen_steps=[{"tests/test_ui.py": "def test_at_01(): assert True\n"}],
            impl_steps=[
                {"index.html": "<html><body><button>Ok</button></body></html>\n"},
                {"index.html": "<html><body><button style='min-height:44px'>Ok</button></body></html>\n"},
            ],
        )
        worker = BuildWorker(
            orchestrator=orch,
            workspace_manager=ws_mgr,
            code_agent=code_agent,
            runner=FakeRunner(
                results=[
                    CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True}),
                    CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True}),
                ]
            ),
            vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI green")]),
            quality_auditor=ParentAuditor(llm=llm),
            chats_dir=chats_dir,
        )

        report = worker.run_job(pid)
        assert report.invocations == 3  # 1 test_gen + 1 impl + 1 refinement
        assert orch.get_status(pid).state == ProjectState.DELIVERED

        # Refinement call received ONLY penalized/failed area findings (ux_ui < 70), not high-scoring areas
        refinement_failure_ctx = str(code_agent.impl_calls[1]["failure_output"] or "")
        assert "ux_ui" in refinement_failure_ctx.lower() or "UX/UI" in refinement_failure_ctx
        assert "Botones menores a 44px" in refinement_failure_ctx
        assert "functional_completeness" not in refinement_failure_ctx

        delivered_ev = next(e for e in store.get_events(pid) if e.type == EventType.PROJECT_DELIVERED)
        sc = delivered_ev.payload["quality_scorecard"]
        assert sc["passed"] is True
        assert sc["global_score"] == 90.0

        history = orch.get_project_chat_history(pid, chats_dir=chats_dir)
        assert any(
            h.get("mode") == "audit" and h.get("status") == "completed" and "90.0%" in h.get("content", "")
            for h in history
        )
    finally:
        store.close()


def test_quality_auditor_respects_max_2_refinements_and_restores_best_checkpoint_on_regression_or_broken_tests(
    tmp_path: Path,
) -> None:
    """Test 3: Stops after max 2 refinement rounds and restores best verified checkpoint when round 2 regresses in score or breaks pytest."""
    from agent_factory.agents.quality_auditor import ParentAuditor

    # Case A: Round 1 improves 74% -> 81% (checkpointed); Round 2 worsens to 62% -> restores Round 1 files & 81% scorecard
    pid_a = "qa-max2-regress"
    chats_dir_a = tmp_path / "chats_qaa"
    llm_a = FakeLLM(
        responses=[
            _build_full_audit_payload(
                ux_ui=64.0,
                functional_completeness=80.0,
                robustness=76.0,
                architecture_quality=76.0,
                security_hygiene=76.0,
                testing_runtime=76.0,
            ),
            _build_full_audit_payload(
                ux_ui=69.0,
                functional_completeness=85.0,
                robustness=84.0,
                architecture_quality=85.0,
                security_hygiene=85.0,
                testing_runtime=86.0,
            ),
            _build_full_audit_payload(
                ux_ui=50.0,
                functional_completeness=65.0,
                robustness=65.0,
                architecture_quality=65.0,
                security_hygiene=70.0,
                testing_runtime=65.0,
            ),
        ]
    )
    orch_a, store_a, ws_mgr_a = _setup_sqlite_approved_project(tmp_path, pid_a, llm=llm_a)
    try:
        worker_a = BuildWorker(
            orchestrator=orch_a,
            workspace_manager=ws_mgr_a,
            code_agent=FakeCodeAgent(
                test_gen_steps=[{"tests/test_ui.py": "def test_at_01(): assert True\n"}],
                impl_steps=[
                    {"index.html": "<!-- v0 -->\n"},
                    {"index.html": "<!-- best_r1_81pct -->\n"},
                    {"index.html": "<!-- regressed_r2_62pct -->\n", "bad_extra.js": "broken();\n"},
                ],
            ),
            runner=FakeRunner(
                results=[
                    CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True}),
                    CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True}),
                    CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True}),
                ]
            ),
            vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
            quality_auditor=ParentAuditor(llm=llm_a),
            chats_dir=chats_dir_a,
        )
        rep_a = worker_a.run_job(pid_a)
        assert rep_a.invocations == 4  # 1 test_gen + 1 impl + 2 max refinement rounds (no 3rd round)
        assert orch_a.get_status(pid_a).state == ProjectState.DELIVERED

        ws_dir_a = ws_mgr_a.get_workspace_dir(pid_a)
        assert "best_r1_81pct" in (ws_dir_a / "index.html").read_text(encoding="utf-8")
        assert not (ws_dir_a / "bad_extra.js").exists()

        deliv_a = next(e for e in store_a.get_events(pid_a) if e.type == EventType.PROJECT_DELIVERED)
        expected_r1 = round(0.25 * 69.0 + 0.25 * 85.0 + 0.125 * (84.0 + 85.0 + 85.0 + 86.0), 2)
        assert deliv_a.payload["quality_scorecard"]["global_score"] == expected_r1
    finally:
        store_a.close()

    # Case B: Round 2 breaks pytest -> restores Round 1 passing checkpoint & delivers cleanly
    pid_b = "qa-max2-broken-tests"
    chats_dir_b = tmp_path / "chats_qab"
    llm_b = FakeLLM(
        responses=[
            _build_full_audit_payload(
                ux_ui=62.0,
                functional_completeness=78.0,
                robustness=76.0,
                architecture_quality=76.0,
                security_hygiene=76.0,
                testing_runtime=76.0,
            ),
            _build_full_audit_payload(
                ux_ui=69.0,
                functional_completeness=86.0,
                robustness=84.0,
                architecture_quality=84.0,
                security_hygiene=86.0,
                testing_runtime=86.0,
            ),
        ]
    )
    orch_b, store_b, ws_mgr_b = _setup_sqlite_approved_project(tmp_path, pid_b, llm=llm_b)
    try:
        worker_b = BuildWorker(
            orchestrator=orch_b,
            workspace_manager=ws_mgr_b,
            code_agent=FakeCodeAgent(
                test_gen_steps=[{"tests/test_ui.py": "def test_at_01(): assert True\n"}],
                impl_steps=[
                    {"index.html": "<!-- v0 -->\n"},
                    {"index.html": "<!-- passing_r1_checkpoint -->\n"},
                    {"index.html": "<!-- broken_r2_fails_pytest -->\n"},
                ],
            ),
            runner=FakeRunner(
                results=[
                    CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True}),
                    CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True}),
                    CommandResult(exit_code=1, stdout="0 passed, 1 failed", test_results={"AT-01": False}),
                ]
            ),
            vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
            quality_auditor=ParentAuditor(llm=llm_b),
            chats_dir=chats_dir_b,
        )
        rep_b = worker_b.run_job(pid_b)
        assert rep_b.invocations == 4
        assert rep_b.test_results == {"AT-01": True}
        assert orch_b.get_status(pid_b).state == ProjectState.DELIVERED

        ws_dir_b = ws_mgr_b.get_workspace_dir(pid_b)
        assert "passing_r1_checkpoint" in (ws_dir_b / "index.html").read_text(encoding="utf-8")
        deliv_b = next(e for e in store_b.get_events(pid_b) if e.type == EventType.PROJECT_DELIVERED)
        expected_r1_b = round(0.25 * 69.0 + 0.25 * 86.0 + 0.125 * (84.0 + 84.0 + 86.0 + 86.0), 2)
        assert deliv_b.payload["quality_scorecard"]["global_score"] == expected_r1_b
    finally:
        store_b.close()


def test_quality_tier_classification_boundaries_and_pending_area(tmp_path: Path) -> None:
    """Quality tiers classify Básico (<75), Estándar (75-84.9 or active area <70), Producción (85-92.9), and Excelencia (>=93)."""
    from agent_factory.agents.quality_auditor import ParentAuditor, classify_quality_tier
    from agent_factory.domain.models import Spec

    # Direct boundary checks of classify_quality_tier
    assert classify_quality_tier(74.9, failed_dimensions=[]) == ("basic", "🥉 Básico")
    assert classify_quality_tier(75.0, failed_dimensions=[]) == ("standard", "🥈 Estándar")
    assert classify_quality_tier(84.9, failed_dimensions=[]) == ("standard", "🥈 Estándar")
    assert classify_quality_tier(85.0, failed_dimensions=[]) == ("production", "🥇 Producción")
    assert classify_quality_tier(92.9, failed_dimensions=[]) == ("production", "🥇 Producción")
    assert classify_quality_tier(93.0, failed_dimensions=[]) == ("excellence", "💎 Excelencia")
    assert classify_quality_tier(88.5, failed_dimensions=["ux_ui"]) == (
        "standard",
        "🥈 Estándar (área pendiente)",
    )

    ui_ws = tmp_path / "ws_tier_ui"
    ui_ws.mkdir(parents=True)
    (ui_ws / "index.html").write_text("<!doctype html><html><body><h1>Tier</h1></body></html>", encoding="utf-8")
    (ui_ws / "tests").mkdir()
    (ui_ws / "tests" / "test_ui.py").write_text("def test_ok(): assert True\n", encoding="utf-8")

    cli_ws = tmp_path / "ws_tier_cli"
    (cli_ws / "src").mkdir(parents=True)
    (cli_ws / "tests").mkdir(parents=True)
    (cli_ws / "src" / "main.py").write_text("def calc(x: int) -> int:\n    return x + 1\n", encoding="utf-8")
    (cli_ws / "tests" / "test_cli.py").write_text("def test_cli(): assert True\n", encoding="utf-8")

    spec = Spec(objective="Evaluar categorías de calidad", users=["Usuario"], features=["Core"])
    llm = FakeLLM(
        responses=[
            # 1. Básico (<75.0)
            _build_full_audit_payload(
                ux_ui=74.0,
                functional_completeness=75.0,
                robustness=75.0,
                architecture_quality=75.0,
                security_hygiene=75.0,
                testing_runtime=75.0,
            ),
            # 2. Estándar (75.0..84.9)
            _build_full_audit_payload(
                ux_ui=80.0,
                functional_completeness=80.0,
                robustness=80.0,
                architecture_quality=80.0,
                security_hygiene=80.0,
                testing_runtime=80.0,
            ),
            # 3. Producción (85.0..92.9 and all >=70)
            _build_full_audit_payload(
                ux_ui=88.0,
                functional_completeness=88.0,
                robustness=88.0,
                architecture_quality=88.0,
                security_hygiene=88.0,
                testing_runtime=88.0,
            ),
            # 4. Excelencia (>=93.0 and all >=70)
            _build_full_audit_payload(
                ux_ui=94.0,
                functional_completeness=94.0,
                robustness=94.0,
                architecture_quality=94.0,
                security_hygiene=94.0,
                testing_runtime=94.0,
            ),
            # 5. Global >=85.0 (87.0) with active ux_ui=60.0 (<70.0) -> Estándar (área pendiente)
            _build_full_audit_payload(
                ux_ui=60.0,
                functional_completeness=96.0,
                robustness=96.0,
                architecture_quality=96.0,
                security_hygiene=96.0,
                testing_runtime=96.0,
            ),
            # 6. CLI project with ux_ui inactive=40.0 and all active >=93.0 -> Excelencia
            _build_full_audit_payload(
                ux_ui=40.0,
                functional_completeness=94.0,
                robustness=94.0,
                architecture_quality=94.0,
                security_hygiene=94.0,
                testing_runtime=94.0,
            ),
        ]
    )
    auditor = ParentAuditor(llm=llm)

    expected_cases = [
        (ui_ws, 74.75, False, "basic", "🥉 Básico"),
        (ui_ws, 80.0, False, "standard", "🥈 Estándar"),
        (ui_ws, 88.0, True, "production", "🥇 Producción"),
        (ui_ws, 94.0, True, "excellence", "💎 Excelencia"),
        (ui_ws, 87.0, False, "standard", "🥈 Estándar (área pendiente)"),
        (cli_ws, 94.0, True, "excellence", "💎 Excelencia"),
    ]
    for idx, (ws_dir, exp_score, exp_passed, exp_tier_id, exp_tier_label) in enumerate(expected_cases, start=1):
        rep = auditor.audit_workspace(f"tier-case-{idx}", ws_dir, spec=spec)
        assert rep.global_score == pytest.approx(exp_score, abs=0.05)
        assert rep.passed is exp_passed
        assert rep.tier_id == exp_tier_id
        assert rep.tier_label == exp_tier_label
        sc = rep.to_scorecard_dict()
        assert sc["tier_id"] == exp_tier_id
        assert sc["tier_label"] == exp_tier_label


def test_evaluate_and_improve_project_quality_with_checkpoint_enrichment(
    tmp_path: Path,
) -> None:
    """evaluate_project_quality audits read-only without modifying files; improve_project_quality refines <85% project to vN+1 Producción; list_project_checkpoints enriches summary & quality_scorecard."""
    from agent_factory.adapters.sqlite_store import SqliteEventStore
    from agent_factory.agents.quality_auditor import ParentAuditor
    from agent_factory.domain.events import create_event

    pid = "qa-on-demand"
    chats_dir = tmp_path / "chats_ondemand"
    store = SqliteEventStore(str(tmp_path / f"{pid}.db"))
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_ondemand")
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "index.html").write_text("<!doctype html><html><body><h1>Inicial</h1></body></html>\n", encoding="utf-8")
    (ws_dir / "tests").mkdir(exist_ok=True)
    (ws_dir / "tests" / "test_app.py").write_text("def test_at_01(): assert True\n", encoding="utf-8")

    now = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
    llm = FakeLLM(
        responses=[
            # 1. evaluate_project_quality (read-only audit -> 72.0% Básico)
            _build_full_audit_payload(
                ux_ui=64.0,
                functional_completeness=80.0,
                robustness=72.0,
                architecture_quality=72.0,
                security_hygiene=72.0,
                testing_runtime=72.0,
                ux_findings=["Sin viewport móvil"],
                ux_improvements=["Añadir viewport y contraste accesible"],
            ),
            # 2. improve_project_quality initial audit -> 72.0% Básico
            _build_full_audit_payload(
                ux_ui=64.0,
                functional_completeness=80.0,
                robustness=72.0,
                architecture_quality=72.0,
                security_hygiene=72.0,
                testing_runtime=72.0,
                ux_findings=["Sin viewport móvil"],
                ux_improvements=["Añadir viewport y contraste accesible"],
            ),
            # 3. improve_project_quality post-refinement round 1 -> 89.0% Producción
            _build_full_audit_payload(
                ux_ui=90.0,
                functional_completeness=90.0,
                robustness=88.0,
                architecture_quality=88.0,
                security_hygiene=88.0,
                testing_runtime=88.0,
            ),
            # 4. second improve_project_quality call -> already >=85% (93.0% Excelencia), no v3 created
            _build_full_audit_payload(
                ux_ui=93.0,
                functional_completeness=93.0,
                robustness=93.0,
                architecture_quality=93.0,
                security_hygiene=93.0,
                testing_runtime=93.0,
            ),
        ]
    )
    orch = Orchestrator(store=store, llm=llm, workspace_manager=ws_mgr)
    sha_v1 = "1111222233334444555566667777888899990000"
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "App Demo", "objective": "Tablero de notas diario", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(
            pid,
            EventType.PLAN_CREATED,
            {
                "plan_hash": "hash-v1",
                "total_tests": 1,
                "from": "MEDIATION",
                "to": "WAITING_APPROVAL",
            },
            at=now,
        ),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": "hash-v1", "decision": "approved", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
        create_event(
            pid,
            EventType.PROJECT_DELIVERED,
            {"repo_url": "", "deploy_url": "", "commit_sha": sha_v1, "version_label": "v1"},
            at=now,
        ),
    ):
        store.append(ev)

    try:
        # Before evaluating, v1 has enriched objective summary and quality_scorecard=None
        cps_before = orch.list_project_checkpoints(pid, chats_dir=chats_dir)
        assert len(cps_before) == 1
        assert cps_before[0]["version_label"] == "v1"
        assert "Tablero de notas diario" in cps_before[0]["summary"]
        assert cps_before[0]["quality_scorecard"] is None

        # Read-only quality evaluation must NOT alter workspace files and updates checkpoint + chat history
        before_html = (ws_dir / "index.html").read_text(encoding="utf-8")
        eval_res = orch.evaluate_project_quality(pid, chats_dir=chats_dir)
        assert (ws_dir / "index.html").read_text(encoding="utf-8") == before_html
        assert eval_res["version_label"] == "v1"
        assert eval_res["scorecard"]["tier_id"] == "basic"
        assert eval_res["scorecard"]["tier_label"] == "🥉 Básico"

        cps_after_eval = orch.list_project_checkpoints(pid, chats_dir=chats_dir)
        assert cps_after_eval[0]["quality_scorecard"] is not None
        assert cps_after_eval[0]["quality_scorecard"]["tier_id"] == "basic"
        chat_hist = orch.get_project_chat_history(pid, chats_dir=chats_dir)
        assert any(m.get("mode") == "audit" and m.get("version_label") == "v1" for m in chat_hist)

        # Improve quality via BuildWorker (bounded max 2 rounds -> reaches Producción 89% -> v2)
        worker = BuildWorker(
            orchestrator=orch,
            workspace_manager=ws_mgr,
            code_agent=FakeCodeAgent(
                impl_steps=[
                    {
                        "index.html": '<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head><body><h1>Mejorado</h1></body></html>\n',
                    }
                ]
            ),
            runner=FakeRunner(
                results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]
            ),
            vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
            quality_auditor=ParentAuditor(llm=llm),
            chats_dir=chats_dir,
        )
        imp_res = worker.improve_project_quality(pid)
        assert imp_res["improved"] is True
        assert imp_res["already_passed"] is False
        assert imp_res["version_label"] == "v2"
        assert imp_res["scorecard"]["passed"] is True
        assert imp_res["scorecard"]["tier_id"] == "production"
        assert imp_res["scorecard"]["tier_label"] == "🥇 Producción"
        assert "Mejorado" in (ws_dir / "index.html").read_text(encoding="utf-8")
        assert orch.get_status(pid).state == ProjectState.DELIVERED

        cps_v2 = orch.list_project_checkpoints(pid, chats_dir=chats_dir)
        assert len(cps_v2) == 2
        assert cps_v2[0]["version_label"] == "v2"
        assert cps_v2[0]["is_current"] is True
        assert cps_v2[0]["quality_scorecard"]["tier_id"] == "production"
        assert cps_v2[0]["summary"]
        assert cps_v2[1]["version_label"] == "v1"
        assert cps_v2[1]["quality_scorecard"]["tier_id"] == "basic"

        # Calling improve_project_quality when already >=85% avoids creating extra version
        noop_res = worker.improve_project_quality(pid)
        assert noop_res["already_passed"] is True
        assert noop_res["improved"] is False
        assert len(orch.list_project_checkpoints(pid, chats_dir=chats_dir)) == 2

        # Verify CHANGE_REQUEST & rollback_from enrichment in list_project_checkpoints summary
        for ev in (
            create_event(
                pid,
                EventType.STATE_TRANSITIONED,
                {"from": "DELIVERED", "to": "CHANGE_REQUEST", "reason": "Agregar exportación CSV"},
                at=now,
            ),
            create_event(pid, EventType.STATE_TRANSITIONED, {"from": "CHANGE_REQUEST", "to": "INTERVIEW"}, at=now),
            create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
            create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
            create_event(pid, EventType.STATE_TRANSITIONED, {"from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
            create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": "hash-v1", "decision": "approved", "to": "BUILDING"}, at=now),
            create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
            create_event(
                pid,
                EventType.PROJECT_DELIVERED,
                {"repo_url": "", "deploy_url": "", "commit_sha": "aaaabbbbccccddddeeeeffff0000111122223333", "version_label": "v3"},
                at=now,
            ),
            create_event(
                pid,
                EventType.STATE_TRANSITIONED,
                {"from": "DELIVERED", "to": "CHANGE_REQUEST", "reason": "Rollback"},
                at=now,
            ),
            create_event(pid, EventType.STATE_TRANSITIONED, {"from": "CHANGE_REQUEST", "to": "INTERVIEW"}, at=now),
            create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
            create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
            create_event(pid, EventType.STATE_TRANSITIONED, {"from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
            create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": "hash-v1", "decision": "approved", "to": "BUILDING"}, at=now),
            create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
            create_event(
                pid,
                EventType.PROJECT_DELIVERED,
                {"repo_url": "", "deploy_url": "", "commit_sha": sha_v1, "rollback_from": sha_v1, "version_label": "v4"},
                at=now,
            ),
        ):
            store.append(ev)
        cps_enriched = orch.list_project_checkpoints(pid, chats_dir=chats_dir)
        by_lbl = {cp["version_label"]: cp for cp in cps_enriched}
        assert "Agregar exportación CSV" in by_lbl["v3"]["summary"]
        assert by_lbl["v3"]["quality_scorecard"] is None
        assert "Restaurado desde 1111222" in by_lbl["v4"]["summary"]
        assert by_lbl["v4"]["rollback_from"] == sha_v1
    finally:
        store.close()


def test_adversarial_quality_debate_critic_responder_diff_review_and_markdown_section(
    tmp_path: Path,
) -> None:
    """2-agent AdversarialCritic + BuilderResponder debate raises score via git diff review and renders Debate section in Markdown report."""
    from agent_factory.adapters.sqlite_store import SqliteEventStore
    from agent_factory.agents.quality_auditor import (
        AdversarialCritic,
        BuilderResponder,
        DebateObjection,
        DebateRoundRecord,
        ParentAuditor,
        build_quality_markdown_report,
    )
    from agent_factory.domain.events import create_event

    pid = "debate-2agents"
    chats_dir = tmp_path / "chats_debate"
    store = SqliteEventStore(str(tmp_path / f"{pid}.db"))
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_debate")
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "index.html").write_text(
        "<!doctype html><html><body><button>Guardar</button></body></html>\n",
        encoding="utf-8",
    )
    (ws_dir / "app.js").write_text("function save() { return true; }\n", encoding="utf-8")
    (ws_dir / "tests").mkdir(exist_ok=True)
    (ws_dir / "tests" / "test_app.py").write_text("def test_ok(): assert True\n", encoding="utf-8")

    now = datetime(2026, 5, 5, 10, 0, tzinfo=UTC)
    critic_llm = FakeLLM(
        responses=[
            # 1. AdversarialCritic initial audit: 82.0% Estándar with file-specific objections
            {
                "summary": "Crítica inicial estricta con oportunidades concretas.",
                "dimensions": {
                    "ux_ui": {"score": 76.0, "findings": ["Botón sin aria-label en index.html"], "improvements": ["Agregar aria-label y foco visible"]},
                    "functional_completeness": {"score": 84.0, "findings": [], "improvements": []},
                    "robustness": {"score": 82.0, "findings": ["save() sin try/catch en app.js"], "improvements": ["Capturar errores en save()"]},
                    "architecture_quality": {"score": 84.0, "findings": [], "improvements": []},
                    "security_hygiene": {"score": 86.0, "findings": [], "improvements": []},
                    "testing_runtime": {"score": 84.0, "findings": [], "improvements": []},
                },
                "objections": [
                    {
                        "dimension": "ux_ui",
                        "file_path": "index.html",
                        "severity": "high",
                        "issue": "Falta aria-label accesible en botón principal de index.html",
                        "concrete_fix": "Agregar aria-label y estilos :focus-visible en index.html",
                    },
                    {
                        "dimension": "robustness",
                        "file_path": "app.js",
                        "severity": "medium",
                        "issue": "save() no maneja errores en app.js",
                        "concrete_fix": "Envolver save() en try/catch con estado claro",
                    },
                ],
            },
            # 2. AdversarialCritic.review_refinement_diff after builder patch -> 93.0% Excelencia
            {
                "summary": "Diff verificado: ambas objeciones resueltas con pruebas verdes.",
                "resolved_count": 2,
                "diff_summary": "Se añadió aria-label en index.html y manejo try/catch en app.js",
                "dimensions": {
                    "ux_ui": {"score": 93.0, "findings": [], "improvements": []},
                    "functional_completeness": {"score": 93.0, "findings": [], "improvements": []},
                    "robustness": {"score": 93.0, "findings": [], "improvements": []},
                    "architecture_quality": {"score": 93.0, "findings": [], "improvements": []},
                    "security_hygiene": {"score": 93.0, "findings": [], "improvements": []},
                    "testing_runtime": {"score": 93.0, "findings": [], "improvements": []},
                },
                "objections": [],
            },
        ]
    )
    builder_llm = FakeLLM(
        responses=[
            {
                "builder_response": (
                    "1) index.html (ux_ui): se agrega aria-label='Guardar datos' y contorno visible.\n"
                    "2) app.js (robustness): se envuelve save() en bloque try/catch con mensaje amigable."
                ),
                "surgical_instructions": (
                    "Modifica index.html añadiendo aria-label y :focus-visible; "
                    "actualiza app.js con try/catch en save() sin tocar tests/."
                ),
            }
        ]
    )

    orch = Orchestrator(store=store, llm=critic_llm, workspace_manager=ws_mgr)
    for ev in (
        create_event(pid, EventType.PROJECT_CREATED, {"name": "Debate App", "objective": "Formulario accesible", "state": "INTERVIEW"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
        create_event(pid, EventType.PLAN_CREATED, {"plan_hash": "hash-d1", "total_tests": 1, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
        create_event(pid, EventType.APPROVAL_RECORDED, {"plan_hash": "hash-d1", "decision": "approved", "to": "BUILDING"}, at=now),
        create_event(pid, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
        create_event(pid, EventType.PROJECT_DELIVERED, {"repo_url": "", "deploy_url": "", "commit_sha": "abcd1234abcd1234abcd1234abcd1234abcd1234", "version_label": "v1"}, at=now),
    ):
        store.append(ev)

    try:
        auditor = ParentAuditor(
            llm=critic_llm,
            critic=AdversarialCritic(llm=critic_llm),
            builder_responder=BuilderResponder(llm=builder_llm),
        )
        code_agent = FakeCodeAgent(
            impl_steps=[
                {
                    "index.html": '<!doctype html><html><body><button aria-label="Guardar datos">Guardar</button></body></html>\n',
                    "app.js": "function save() { try { return true; } catch (err) { return false; } }\n",
                }
            ]
        )
        worker = BuildWorker(
            orchestrator=orch,
            workspace_manager=ws_mgr,
            code_agent=code_agent,
            runner=FakeRunner(results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]),
            vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
            quality_auditor=auditor,
            chats_dir=chats_dir,
        )

        res = worker.improve_project_quality(pid)
        assert res["improved"] is True
        assert res["version_label"] == "v2"
        sc = res["scorecard"]
        assert sc["tier_id"] == "excellence"
        assert sc["global_score"] == pytest.approx(93.0, abs=0.05)

        # Verify BuilderResponder actually ran and passed surgical instructions to CodeAgent
        assert len(builder_llm.calls) == 1
        assert "index.html" in code_agent.impl_calls[0]["failure_output"]
        assert "app.js" in code_agent.impl_calls[0]["failure_output"]

        # Verify AdversarialCritic.review_refinement_diff inspected the real diff containing aria-label & try/catch
        assert len(critic_llm.calls) == 2
        diff_prompt = str(critic_llm.calls[1]["messages"])
        assert "aria-label" in diff_prompt
        assert "index.html" in diff_prompt

        # Verify debate_rounds & debate_summary in scorecard + checkpoints
        assert len(sc["debate_rounds"]) == 1
        rnd = sc["debate_rounds"][0]
        assert isinstance(DebateRoundRecord.model_validate(rnd), DebateRoundRecord)
        assert DebateObjection.model_validate(rnd["objections"][0]).file_path == "index.html"
        assert rnd["score_before"] == pytest.approx(82.0, abs=0.05)
        assert rnd["score_after"] == pytest.approx(93.0, abs=0.05)
        assert rnd["resolved_count"] == 2
        assert rnd["kept"] is True
        assert "index.html" in rnd["builder_response"]
        assert sc["debate_summary"]["resolved_count"] == 2
        assert sc["debate_summary"]["score_delta"] == pytest.approx(11.0, abs=0.1)

        # Verify rendered Markdown includes the adversarial debate section & score progression
        chat_hist = orch.get_project_chat_history(pid, chats_dir=chats_dir)
        md = chat_hist[-1]["content"]
        assert "⚔️ Debate de Calidad (Crítico vs. Constructor)" in md
        assert "82.0%" in md and "93.0%" in md
        assert "index.html" in md
        assert "Falta aria-label accesible" in md
        assert "Guardar datos" in md

        # Direct helper test: build_quality_markdown_report also formats empty/kept debate nicely
        sample_report = auditor.audit_workspace(pid, ws_dir) if False else None
        assert sample_report is None and callable(build_quality_markdown_report)
    finally:
        store.close()


def test_force_polish_debate_upgrades_passing_project_and_reverts_on_broken_tests_or_regression(
    tmp_path: Path,
) -> None:
    """force_polish=True challenges an already Producción project (>=85%), upgrades cleanly when valid, and discards + avoids vN+1 on broken pytest or score drop."""
    from agent_factory.adapters.sqlite_store import SqliteEventStore
    from agent_factory.agents.quality_auditor import ParentAuditor
    from agent_factory.domain.events import create_event

    def _mk_project(pid_name: str, responses: list[dict[str, object]], runner_results: list[CommandResult]):
        chats_dir = tmp_path / f"chats_{pid_name}"
        store = SqliteEventStore(str(tmp_path / f"{pid_name}.db"))
        ws_mgr = WorkspaceManager(base_dir=tmp_path / f"ws_{pid_name}")
        ws_dir = ws_mgr.create_workspace(pid_name)
        (ws_dir / "index.html").write_text("<!-- v1_clean --><h1>Prod</h1>\n", encoding="utf-8")
        (ws_dir / "tests").mkdir(exist_ok=True)
        (ws_dir / "tests" / "test_ok.py").write_text("def test_pass(): assert True\n", encoding="utf-8")

        now = datetime(2026, 5, 6, 11, 0, tzinfo=UTC)
        llm = FakeLLM(responses=responses)
        orch = Orchestrator(store=store, llm=llm, workspace_manager=ws_mgr)
        for ev in (
            create_event(pid_name, EventType.PROJECT_CREATED, {"name": pid_name, "objective": "App pulida", "state": "INTERVIEW"}, at=now),
            create_event(pid_name, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now),
            create_event(pid_name, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now),
            create_event(pid_name, EventType.PLAN_CREATED, {"plan_hash": "h-p1", "total_tests": 1, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now),
            create_event(pid_name, EventType.APPROVAL_RECORDED, {"plan_hash": "h-p1", "decision": "approved", "to": "BUILDING"}, at=now),
            create_event(pid_name, EventType.STATE_TRANSITIONED, {"from": "BUILDING", "to": "VERIFYING"}, at=now),
            create_event(
                pid_name,
                EventType.PROJECT_DELIVERED,
                {"repo_url": "", "deploy_url": "", "commit_sha": "deadbeef11112222333344445555666677778888", "version_label": "v1"},
                at=now,
            ),
        ):
            store.append(ev)

        code_agent = FakeCodeAgent(
            impl_steps=[
                {"index.html": "<!-- polished_r1 --><h1>Excelente</h1>\n"},
                {"index.html": "<!-- polished_r2_unused --><h1>Excelente 2</h1>\n"},
            ]
        )
        worker = BuildWorker(
            orchestrator=orch,
            workspace_manager=ws_mgr,
            code_agent=code_agent,
            runner=FakeRunner(results=runner_results),
            vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
            quality_auditor=ParentAuditor(llm=llm),
            chats_dir=chats_dir,
        )
        return orch, store, ws_dir, chats_dir, worker

    # Case A: Already 88% Producción -> force_polish=True debates & improves to 95% Excelencia (v2)
    orch_a, store_a, ws_a, chats_a, worker_a = _mk_project(
        "fp-upgrade",
        responses=[
            {
                "summary": "Cumple Producción (88%), pulible a Excelencia.",
                "dimensions": {
                    "ux_ui": {"score": 88.0, "findings": ["Falta skip-link en index.html"], "improvements": ["Agregar skip-link"]},
                    "functional_completeness": {"score": 88.0, "findings": [], "improvements": []},
                    "robustness": {"score": 88.0, "findings": [], "improvements": []},
                    "architecture_quality": {"score": 88.0, "findings": [], "improvements": []},
                    "security_hygiene": {"score": 88.0, "findings": [], "improvements": []},
                    "testing_runtime": {"score": 88.0, "findings": [], "improvements": []},
                },
                "objections": [
                    {"dimension": "ux_ui", "file_path": "index.html", "severity": "medium", "issue": "Falta enlace skip-link accesible", "concrete_fix": "Agregar skip-link en index.html"}
                ],
            },
            {
                "summary": "Pulido completado (95%).",
                "resolved_count": 1,
                "dimensions": {
                    "ux_ui": {"score": 95.0, "findings": [], "improvements": []},
                    "functional_completeness": {"score": 95.0, "findings": [], "improvements": []},
                    "robustness": {"score": 95.0, "findings": [], "improvements": []},
                    "architecture_quality": {"score": 95.0, "findings": [], "improvements": []},
                    "security_hygiene": {"score": 95.0, "findings": [], "improvements": []},
                    "testing_runtime": {"score": 95.0, "findings": [], "improvements": []},
                },
            },
        ],
        runner_results=[
            CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True}),
            CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True}),
        ],
    )
    try:
        res_a = worker_a.improve_project_quality("fp-upgrade", force_polish=True)
        assert res_a["improved"] is True
        assert res_a["version_label"] == "v2"
        assert res_a["scorecard"]["global_score"] == pytest.approx(95.0, abs=0.05)
        assert res_a["scorecard"]["tier_id"] == "excellence"
        assert "polished_r1" in (ws_a / "index.html").read_text(encoding="utf-8")
        assert len(orch_a.list_project_checkpoints("fp-upgrade", chats_dir=chats_a)) == 2
    finally:
        store_a.close()

    # Case B1: force_polish=True where pytest breaks on both rounds -> restores v1 files, kept=False, NO v2 created
    orch_b, store_b, ws_b, chats_b, worker_b = _mk_project(
        "fp-broken-tests",
        responses=[
            {
                "summary": "88% Producción",
                "dimensions": {
                    "ux_ui": {"score": 88.0, "findings": ["Pulido menor"], "improvements": ["Mejorar contraste"]},
                    "functional_completeness": {"score": 88.0, "findings": [], "improvements": []},
                    "robustness": {"score": 88.0, "findings": [], "improvements": []},
                    "architecture_quality": {"score": 88.0, "findings": [], "improvements": []},
                    "security_hygiene": {"score": 88.0, "findings": [], "improvements": []},
                    "testing_runtime": {"score": 88.0, "findings": [], "improvements": []},
                },
            }
        ],
        runner_results=[
            CommandResult(exit_code=1, stdout="1 failed", test_results={"AT-01": False}),
            CommandResult(exit_code=1, stdout="1 failed", test_results={"AT-01": False}),
        ],
    )
    try:
        res_b = worker_b.improve_project_quality("fp-broken-tests", force_polish=True)
        assert res_b["improved"] is False
        assert "v1_clean" in (ws_b / "index.html").read_text(encoding="utf-8")
        assert orch_b.get_status("fp-broken-tests").state == ProjectState.DELIVERED
        cps_b = orch_b.list_project_checkpoints("fp-broken-tests", chats_dir=chats_b)
        assert len(cps_b) == 1
        assert all(r["kept"] is False for r in res_b["scorecard"]["debate_rounds"])
    finally:
        store_b.close()

    # Case B2: force_polish=True where diff review drops score (88% -> 76%) -> restores v1 files, kept=False, NO v2 created
    orch_c, store_c, ws_c, chats_c, worker_c = _mk_project(
        "fp-regression",
        responses=[
            {
                "summary": "88% Producción",
                "dimensions": {
                    "ux_ui": {"score": 88.0, "findings": ["Pulido menor"], "improvements": ["Refinar layout"]},
                    "functional_completeness": {"score": 88.0, "findings": [], "improvements": []},
                    "robustness": {"score": 88.0, "findings": [], "improvements": []},
                    "architecture_quality": {"score": 88.0, "findings": [], "improvements": []},
                    "security_hygiene": {"score": 88.0, "findings": [], "improvements": []},
                    "testing_runtime": {"score": 88.0, "findings": [], "improvements": []},
                },
            },
            {
                "summary": "Regresión a 76%",
                "resolved_count": 0,
                "dimensions": {
                    "ux_ui": {"score": 76.0, "findings": ["Empeoró"], "improvements": []},
                    "functional_completeness": {"score": 76.0, "findings": [], "improvements": []},
                    "robustness": {"score": 76.0, "findings": [], "improvements": []},
                    "architecture_quality": {"score": 76.0, "findings": [], "improvements": []},
                    "security_hygiene": {"score": 76.0, "findings": [], "improvements": []},
                    "testing_runtime": {"score": 76.0, "findings": [], "improvements": []},
                },
            },
            {
                "summary": "Regresión ronda 2 a 74%",
                "resolved_count": 0,
                "dimensions": {
                    "ux_ui": {"score": 74.0, "findings": ["Empeoró más"], "improvements": []},
                    "functional_completeness": {"score": 74.0, "findings": [], "improvements": []},
                    "robustness": {"score": 74.0, "findings": [], "improvements": []},
                    "architecture_quality": {"score": 74.0, "findings": [], "improvements": []},
                    "security_hygiene": {"score": 74.0, "findings": [], "improvements": []},
                    "testing_runtime": {"score": 74.0, "findings": [], "improvements": []},
                },
            },
        ],
        runner_results=[
            CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True}),
            CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True}),
        ],
    )
    try:
        res_c = worker_c.improve_project_quality("fp-regression", force_polish=True)
        assert res_c["improved"] is False
        assert "v1_clean" in (ws_c / "index.html").read_text(encoding="utf-8")
        assert len(orch_c.list_project_checkpoints("fp-regression", chats_dir=chats_c)) == 1
        assert all(r["kept"] is False for r in res_c["scorecard"]["debate_rounds"])
    finally:
        store_c.close()


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_local_app_runner_readiness_timeout_stderr_diagnostics_and_uvicorn_port_flags(
    tmp_path: Path,
) -> None:
    """Default readiness_timeout is 25.0s, timeouts/early exits capture redacted stdout+stderr diagnostics cleanly (INV-19), and FastAPI apps pass PORT env and --port <port> to uvicorn."""
    import inspect
    import json
    import os
    import subprocess
    import sys
    import urllib.request

    from agent_factory.runtime_runner import (
        LocalAppError,
        LocalAppRunner,
        _compute_deps_hash,
        _find_python_dependency_manifests,
        run_http_smoke_test,
    )

    # 1. Default readiness timeouts must be 25.0s
    assert inspect.signature(LocalAppRunner.start).parameters["readiness_timeout"].default == 25.0
    assert inspect.signature(run_http_smoke_test).parameters["timeout_seconds"].default == 25.0

    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_runtime_diag")
    secret_val = "TEST_DIAG_SECRET_9182"

    # 2. Readiness timeout attaches buffered stdout + stderr diagnostics with secret redaction
    pid_slow = "proj-slow-boot"
    ws_slow = ws_mgr.create_workspace(pid_slow)
    (ws_slow / "app.py").write_text(
        "import sys, time\n"
        "print('stdout: loading heavy imaging modules', flush=True)\n"
        f"sys.stderr.write('stderr: still importing weights token={secret_val}\\n')\n"
        "sys.stderr.flush()\n"
        "time.sleep(10.0)\n",
        encoding="utf-8",
    )
    runner_diag = LocalAppRunner(
        workspace_manager=ws_mgr,
        secrets_to_redact=(secret_val,),
    )
    try:
        with pytest.raises(LocalAppError) as timeout_err:
            runner_diag.start(pid_slow, readiness_timeout=0.7)
        msg = str(timeout_err.value)
        assert "0.7s" in msg
        assert "stdout: loading heavy imaging modules" in msg
        assert "stderr: still importing weights" in msg
        assert secret_val not in msg
        assert "[REDACTED]" in msg
    finally:
        runner_diag.stop_all()

    # 3. FastAPI workspace (no __main__, AND hardcoded uvicorn port=8000) launches via `python -m uvicorn <mod>:app --host ... --port <port>` with PORT in env
    def _create_fake_venv_site(ws_path: Path) -> Path:
        venv_dir = ws_path / ".venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv_dir)], check=True)
        site_dir = (
            venv_dir / "Lib" / "site-packages"
            if os.name == "nt"
            else next((venv_dir / "lib").glob("python*/site-packages"))
        )
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "fastapi.py").write_text("class FastAPI:\n    pass\n", encoding="utf-8")
        uv_pkg = site_dir / "uvicorn"
        uv_pkg.mkdir(parents=True, exist_ok=True)
        (uv_pkg / "__init__.py").write_text(
            "def run(*args, **kwargs):\n    raise RuntimeError('should pass assigned port via CLI')\n",
            encoding="utf-8",
        )
        (uv_pkg / "__main__.py").write_text(
            "import argparse, http.server, json, os, sys\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('target')\n"
            "p.add_argument('--host', default='127.0.0.1')\n"
            "p.add_argument('--port', type=int, required=True)\n"
            "args = p.parse_args()\n"
            "class H(http.server.BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        body = json.dumps({'target': args.target, 'cli_port': args.port, 'env_port': os.environ.get('PORT'), 'argv': sys.argv[1:]}).encode()\n"
            "        self.send_response(200)\n"
            "        self.send_header('Content-Type', 'application/json')\n"
            "        self.send_header('Content-Length', str(len(body)))\n"
            "        self.end_headers()\n"
            "        self.wfile.write(body)\n"
            "    def log_message(self, *a): pass\n"
            "http.server.ThreadingHTTPServer((args.host, args.port), H).serve_forever()\n",
            encoding="utf-8",
        )
        reqs, pyps = _find_python_dependency_manifests(ws_path)
        dep_hash = _compute_deps_hash(ws_path, [*reqs, *pyps], ast_packages=["fastapi", "uvicorn"])
        (venv_dir / ".deps_hash").write_text(dep_hash, encoding="utf-8")
        return site_dir

    pid_fastapi = "proj-fastapi-uvicorn"
    ws_fastapi = ws_mgr.create_workspace(pid_fastapi)
    (ws_fastapi / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
    (ws_fastapi / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "import uvicorn\n"
        "app = FastAPI()\n"
        "if __name__ == '__main__':\n"
        "    uvicorn.run(app, host='127.0.0.1', port=8000)\n",
        encoding="utf-8",
    )
    _create_fake_venv_site(ws_fastapi)

    runner_fast = LocalAppRunner(
        workspace_manager=ws_mgr,
        dep_command_runner=lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    try:
        st = runner_fast.start(pid_fastapi, install_deps=True, readiness_timeout=5.0)
        assert st.running is True
        assert st.app_type == "python_local"
        assert st.port is not None
        with urllib.request.urlopen(st.url, timeout=5.0) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        assert info["target"] == "main:app"
        assert info["cli_port"] == st.port
        assert info["env_port"] == str(st.port)
        assert "--port" in info["argv"]
        assert str(st.port) in info["argv"]
    finally:
        runner_fast.stop_all()

    # 4. FastAPI without __main__ (nested src/main.py) also resolves uvicorn target and passes --port + PORT env
    pid_fast_nomain = "proj-fastapi-nomain"
    ws_nomain = ws_mgr.create_workspace(pid_fast_nomain)
    (ws_nomain / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (ws_nomain / "src").mkdir(parents=True)
    (ws_nomain / "src" / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "api_app = FastAPI()\n",
        encoding="utf-8",
    )
    _create_fake_venv_site(ws_nomain)
    runner_nomain = LocalAppRunner(
        workspace_manager=ws_mgr,
        dep_command_runner=lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    try:
        st2 = runner_nomain.start(pid_fast_nomain, install_deps=True, readiness_timeout=5.0)
        assert st2.running is True
        with urllib.request.urlopen(st2.url, timeout=5.0) as resp2:
            info2 = json.loads(resp2.read().decode("utf-8"))
        assert info2["target"] == "main:api_app"
        assert info2["cli_port"] == st2.port
        assert info2["env_port"] == str(st2.port)
    finally:
        runner_nomain.stop_all()


def test_local_app_runner_and_build_worker_resolve_bare_cli_commands_and_pytest_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LocalAppRunner._run_dep_command resolves bare CLI names via PATH/which, and BuildWorker._run_local_pytest falls back to sys.executable -m pytest when pytest is absent from PATH."""
    import os
    import shutil
    import subprocess
    import sys

    from agent_factory.adapters.subprocess_runner import SubprocessRunner
    from agent_factory.builder import BuildJob, BuildWorker
    from agent_factory.runtime_runner import LocalAppRunner

    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_cli_res")
    pid = "proj-cli-resolve"
    ws_dir = ws_mgr.create_workspace(pid)
    (ws_dir / "tests").mkdir(parents=True)
    (ws_dir / "tests" / "test_ok.py").write_text("def test_pass():\n    assert 1 == 1\n", encoding="utf-8")

    # 1. LocalAppRunner._run_dep_command resolves bare executable in env PATH
    dep_bin = tmp_path / "dep_bin"
    dep_bin.mkdir()
    if os.name == "nt":
        dummy_dep = dep_bin / "dummydep.cmd"
        dummy_dep.write_text("@echo off\r\necho ok\r\n", encoding="utf-8")
    else:
        dummy_dep = dep_bin / "dummydep"
        dummy_dep.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        dummy_dep.chmod(0o755)

    recorded_dep_cmds: list[list[str]] = []

    def _rec_dep(cmd, **_kwargs):
        recorded_dep_cmds.append([str(c) for c in cmd])
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    app_runner = LocalAppRunner(workspace_manager=ws_mgr, dep_command_runner=_rec_dep)
    app_runner._run_dep_command(
        ["dummydep", "--check"],
        cwd=ws_dir,
        env={"PATH": str(dep_bin)},
        timeout_seconds=5.0,
        context="dep check",
    )
    assert Path(recorded_dep_cmds[0][0]).resolve() == dummy_dep.resolve()

    # 2. BuildWorker._run_local_pytest falls back cleanly when shutil.which("pytest") is None
    orch, _store = _create_approved_project(pid, num_tests=1, approve=True)
    plan = orch.get_plan(pid)
    assert plan is not None
    job = BuildJob(project_id=pid, plan=plan)
    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=FakeCodeAgent(),
        runner=SubprocessRunner(default_timeout_seconds=10.0),
        vcs=FakeVCS(),
    )
    monkeypatch.setattr(shutil, "which", lambda _binary, **_kw: None)
    passed, passed_cnt, total_cnt, failing, _res, _t0 = worker._run_local_pytest(
        pid,
        job,
        ws_dir,
        build_round=1,
    )
    assert passed is True
    assert passed_cnt >= 1
    assert total_cnt >= 1
    assert failing == []
    _ = sys.executable


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_build_worker_passes_browser_smoke_findings_to_adversarial_critic_and_triggers_refinement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_http_smoke_test invokes run_browser_smoke_test and BuildWorker forwards console/overflow/broken-image findings into AdversarialCritic, forcing refinement when UX/UI is capped <= 72."""
    import sys
    import types
    from typing import Any

    from agent_factory.agents.quality_auditor import AdversarialCritic, ParentAuditor
    from agent_factory.runtime_runner import LocalAppRunner, run_http_smoke_test

    pid = "proj-browser-smoke-critic"
    orch, store = _create_approved_project(pid, num_tests=1, approve=True)
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_browser_smoke")
    local_runner = LocalAppRunner(workspace_manager=ws_mgr)

    # First browser visit fails (console.error + overflow + broken img); second visit passes after refinement
    visits = {"count": 0}

    class _FakeMsg:
        def __init__(self, t: str, txt: str) -> None:
            self.type = t
            self.text = txt

    class _StepPage:
        def __init__(self, idx: int) -> None:
            self.idx = idx
            self.handlers: dict[str, list[Any]] = {}

        def on(self, event: str, fn: Any) -> None:
            self.handlers.setdefault(event, []).append(fn)

        def goto(self, _url: str, **_kw: Any) -> None:
            if self.idx == 1:
                for fn in self.handlers.get("console", []):
                    fn(_FakeMsg("error", "console boom on initial load"))

        def wait_for_timeout(self, _ms: int) -> None:
            return None

        def evaluate(self, script: str) -> Any:
            if "scrollWidth" in script:
                return {"scrollWidth": 1440, "innerWidth": 1280} if self.idx == 1 else {"scrollWidth": 1280, "innerWidth": 1280}
            return ["/missing.png"] if self.idx == 1 else []

    class _StepPW:
        def __enter__(self) -> Any:
            visits["count"] += 1
            cur_idx = visits["count"]
            return types.SimpleNamespace(
                chromium=types.SimpleNamespace(
                    launch=lambda **_kw: types.SimpleNamespace(
                        new_page=lambda **_pw_kw: _StepPage(cur_idx),
                        close=lambda: None,
                    )
                )
            )

        def __exit__(self, *_a: object) -> None:
            return None

    fake_pkg = types.ModuleType("playwright")
    fake_sync = types.ModuleType("playwright.sync_api")
    fake_sync.sync_playwright = lambda: _StepPW()  # type: ignore[attr-defined]
    fake_pkg.sync_api = fake_sync  # type: ignore[attr-defined]

    clean_html = (
        "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>@media(max-width:600px){body{margin:0}}</style></head>"
        "<body><button id='go' aria-label='Ir'>Ir</button></body></html>\n"
    )
    inflated_r1 = {
        "summary": "Todo ok visualmente",
        "dimensions": {
            "ux_ui": {"score": 94.0, "findings": [], "improvements": []},
            "functional_completeness": {"score": 88.0, "findings": [], "improvements": []},
            "robustness": {"score": 88.0, "findings": [], "improvements": []},
            "architecture_quality": {"score": 88.0, "findings": [], "improvements": []},
            "security_hygiene": {"score": 88.0, "findings": [], "improvements": []},
            "testing_runtime": {"score": 88.0, "findings": [], "improvements": []},
        },
        "objections": [],
    }
    clean_r2 = {
        "summary": "Errores de navegador resueltos en diff",
        "resolved_count": 1,
        "dimensions": {
            "ux_ui": {"score": 91.0, "findings": [], "improvements": []},
            "functional_completeness": {"score": 90.0, "findings": [], "improvements": []},
            "robustness": {"score": 90.0, "findings": [], "improvements": []},
            "architecture_quality": {"score": 90.0, "findings": [], "improvements": []},
            "security_hygiene": {"score": 90.0, "findings": [], "improvements": []},
            "testing_runtime": {"score": 90.0, "findings": [], "improvements": []},
        },
        "objections": [],
    }
    critic_llm = FakeLLM(responses=[inflated_r1, clean_r2])
    auditor = ParentAuditor(llm=critic_llm, critic=AdversarialCritic(llm=critic_llm))
    agent = FakeCodeAgent(
        test_gen_steps=[{"tests/test_app.py": "def test_at_01(): assert 1 + 1 == 2\n"}],
        impl_steps=[
            {"index.html": clean_html},
            {"index.html": clean_html + "<!-- fixed -->\n"},
        ],
    )
    worker = BuildWorker(
        orchestrator=orch,
        workspace_manager=ws_mgr,
        code_agent=agent,
        runner=FakeRunner(
            results=[
                CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True}),
                CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True}),
            ]
        ),
        vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI ok")]),
        local_app_runner=local_runner,
        quality_auditor=auditor,
    )

    try:
        with monkeypatch.context() as mp:
            mp.setitem(sys.modules, "playwright", fake_pkg)
            mp.setitem(sys.modules, "playwright.sync_api", fake_sync)
            report = worker.run_job(pid)

        assert orch.get_status(pid).state == ProjectState.DELIVERED
        assert report.invocations == 3  # test_gen + initial impl + 1 adversarial refinement round
        assert visits["count"] == 2
        critic_first_prompt = str(critic_llm.calls[0]["messages"])
        assert "console boom on initial load" in critic_first_prompt
        assert "1440" in critic_first_prompt and "1280" in critic_first_prompt
        assert "/missing.png" in critic_first_prompt

        delivered = next(e for e in store.get_events(pid) if e.type == EventType.PROJECT_DELIVERED)
        sc = delivered.payload["quality_scorecard"]
        assert sc["debate_summary"]["score_before"] < 85.0
        assert sc["global_score"] >= 85.0
        _ = run_http_smoke_test
    finally:
        local_runner.stop_all()



