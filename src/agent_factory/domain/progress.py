"""Deterministic progress calculation and event-log status projection (INV-11, INV-12)."""

from collections.abc import Sequence

from agent_factory.domain.events import Event, EventType
from agent_factory.domain.models import ProjectStatus, TestCounts
from agent_factory.domain.states import ProjectState

STATE_BASE_PROGRESS: dict[ProjectState, int] = {
    ProjectState.IDLE: 0,
    ProjectState.INTERVIEW: 10,
    ProjectState.DEBATE: 25,
    ProjectState.MEDIATION: 30,
    ProjectState.WAITING_APPROVAL: 35,
    ProjectState.BUILDING: 35,
    ProjectState.VERIFYING: 95,
    ProjectState.DELIVERED: 100,
    ProjectState.CHANGE_REQUEST: 10,
}


def calculate_progress(
    state: ProjectState,
    *,
    passed_tests: int = 0,
    total_tests: int = 0,
    previous_progress: int = 0,
) -> int:
    """Calculate deterministic progress percentage for a state (INV-12, Section 5)."""
    if state == ProjectState.FAILED:
        return previous_progress
    if state == ProjectState.BUILDING:
        if total_tests <= 0:
            return 35
        ratio = min(max(passed_tests / total_tests, 0.0), 1.0)
        return round(35 + (90 - 35) * ratio)
    return STATE_BASE_PROGRESS.get(state, 0)


def project_status_from_events(project_id: str, events: Sequence[Event]) -> ProjectStatus:
    """Rebuild ProjectStatus deterministically from the append-only event log (INV-11)."""
    name = ""
    objective = ""
    current_state = ProjectState.IDLE
    plan_hash: str | None = None
    tests = TestCounts()
    agent_invocations = 0
    ci_rounds = 0
    failure_reason: str | None = None
    last_event: str | None = None
    updated_at = None
    last_progress = 0

    for event in events:
        last_event = event.id
        updated_at = event.at
        payload = event.payload or {}

        if event.type == EventType.PROJECT_CREATED:
            name = payload.get("name", name)
            objective = payload.get("objective", objective)
            raw_state = payload.get("state", ProjectState.INTERVIEW)
            current_state = ProjectState(raw_state)
            last_progress = calculate_progress(
                current_state,
                passed_tests=tests.passed,
                total_tests=tests.total,
                previous_progress=last_progress,
            )

        elif event.type == EventType.STATE_TRANSITIONED:
            current_state = ProjectState(payload["to"])
            last_progress = calculate_progress(
                current_state,
                passed_tests=tests.passed,
                total_tests=tests.total,
                previous_progress=last_progress,
            )

        elif event.type == EventType.PLAN_CREATED:
            plan_hash = payload.get("plan_hash", plan_hash)
            if "total_tests" in payload:
                tests.total = payload["total_tests"]
            if "to" in payload:
                current_state = ProjectState(payload["to"])
            last_progress = calculate_progress(
                current_state,
                passed_tests=tests.passed,
                total_tests=tests.total,
                previous_progress=last_progress,
            )

        elif event.type == EventType.APPROVAL_RECORDED:
            plan_hash = payload.get("plan_hash", plan_hash)
            if "to" in payload:
                current_state = ProjectState(payload["to"])
            last_progress = calculate_progress(
                current_state,
                passed_tests=tests.passed,
                total_tests=tests.total,
                previous_progress=last_progress,
            )

        elif event.type == EventType.TESTS_UPDATED:
            tests.passed = payload.get("passed", tests.passed)
            tests.failed = payload.get("failed", tests.failed)
            tests.total = payload.get("total", tests.total)
            last_progress = calculate_progress(
                current_state,
                passed_tests=tests.passed,
                total_tests=tests.total,
                previous_progress=last_progress,
            )

        elif event.type == EventType.AGENT_INVOKED:
            agent_invocations += 1

        elif event.type == EventType.CI_ROUND_COMPLETED:
            ci_rounds = payload.get("ci_rounds", ci_rounds + 1)

        elif event.type == EventType.PROJECT_FAILED:
            current_state = ProjectState.FAILED
            failure_reason = payload.get("reason", "Unknown failure")
            # FAILED preserves the last progress value

        elif event.type == EventType.PROJECT_DELIVERED:
            current_state = ProjectState.DELIVERED
            last_progress = 100

    return ProjectStatus(
        project_id=project_id,
        name=name,
        objective=objective,
        state=current_state,
        phase=current_state.value,
        progress_pct=last_progress,
        plan_hash=plan_hash,
        tests=tests,
        agent_invocations=agent_invocations,
        ci_rounds=ci_rounds,
        last_event=last_event,
        updated_at=updated_at,
        failure_reason=failure_reason,
    )
