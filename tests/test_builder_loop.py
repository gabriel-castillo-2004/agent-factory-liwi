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
from agent_factory.ports.runner import CommandResult
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
