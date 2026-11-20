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





