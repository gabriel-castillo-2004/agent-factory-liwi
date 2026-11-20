"""Deterministic progress calculation and event-log status projection (INV-11, INV-12)."""

from collections.abc import Sequence

from agent_factory.domain.events import Event, EventType
from agent_factory.domain.models import ProjectLinks, ProjectStatus, TestCounts
from agent_factory.domain.states import ProjectState, validate_transition

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
    links = ProjectLinks()
    workspace_path: str | None = None

    for event in events:
        last_event = event.id
        updated_at = event.at
        payload = event.payload or {}

        if current_state == ProjectState.IDLE and event.type != EventType.PROJECT_CREATED:
            if event.type == EventType.PROJECT_FAILED:
                validate_transition(current_state, ProjectState.FAILED)
            raise ValueError(
                f"Invalid event log: cannot process '{event.type}' prior to PROJECT_CREATED"
            )

        if event.type == EventType.PROJECT_CREATED:
            name = payload.get("name", name)
            objective = payload.get("objective", objective)
            raw_ext_ws = payload.get("external_workspace_path")
            if raw_ext_ws:
                workspace_path = str(raw_ext_ws).strip() or workspace_path
            raw_state = payload.get("state", ProjectState.INTERVIEW)
            next_state = ProjectState(raw_state)
            validate_transition(current_state, next_state)
            current_state = next_state
            last_progress = calculate_progress(
                current_state,
                passed_tests=tests.passed,
                total_tests=tests.total,
                previous_progress=last_progress,
            )

        elif event.type == EventType.PROJECT_RENAMED:
            new_name = str(payload.get("name", "")).strip()
            if new_name:
                name = new_name
            if "repo_url" in payload or "deploy_url" in payload:
                links = ProjectLinks(
                    repo=payload.get("repo_url", links.repo),
                    deploy=payload.get("deploy_url", links.deploy),
                )

        elif event.type == EventType.STATE_TRANSITIONED:
            next_state = ProjectState(payload["to"])
            if next_state == ProjectState.BUILDING and current_state != ProjectState.VERIFYING:
                raise ValueError(
                    "Cannot transition to BUILDING via STATE_TRANSITIONED without valid APPROVAL_RECORDED"
                )
            validate_transition(current_state, next_state)
            current_state = next_state
            last_progress = calculate_progress(
                current_state,
                passed_tests=tests.passed,
                total_tests=tests.total,
                previous_progress=last_progress,
            )

        elif event.type == EventType.PLAN_CREATED:
            if current_state not in (ProjectState.DEBATE, ProjectState.MEDIATION):
                raise ValueError(
                    f"Cannot create plan in state {current_state}: PLAN_CREATED only valid in DEBATE or MEDIATION"
                )
            next_state = ProjectState(payload.get("to", ProjectState.WAITING_APPROVAL))
            validate_transition(current_state, next_state)
            current_state = next_state
            plan_hash = payload.get("plan_hash", plan_hash)
            if "total_tests" in payload:
                tests.total = payload["total_tests"]
            last_progress = calculate_progress(
                current_state,
                passed_tests=tests.passed,
                total_tests=tests.total,
                previous_progress=last_progress,
            )

        elif event.type == EventType.APPROVAL_RECORDED:
            target_to = payload.get("to", ProjectState.BUILDING)
            next_state = ProjectState(target_to) if not isinstance(target_to, ProjectState) else target_to
            if next_state != ProjectState.BUILDING:
                raise ValueError(f"APPROVAL_RECORDED cannot transition to '{next_state}': must target BUILDING")
            if current_state != ProjectState.WAITING_APPROVAL:
                raise ValueError(f"Cannot record approval in state '{current_state}': must be WAITING_APPROVAL")
            decision = payload.get("decision")
            if decision != "approved":
                raise ValueError(f"Cannot transition to BUILDING: approval decision is '{decision}'")
            approved_hash = payload.get("plan_hash")
            if not plan_hash or approved_hash != plan_hash:
                raise ValueError(
                    f"Cannot transition to BUILDING: plan_hash mismatch ('{approved_hash}' != '{plan_hash}')"
                )
            validate_transition(current_state, next_state)
            current_state = next_state
            plan_hash = approved_hash
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
            validate_transition(current_state, ProjectState.FAILED)
            current_state = ProjectState.FAILED
            failure_reason = payload.get("reason", "Unknown failure")
            # FAILED preserves the last progress value

        elif event.type == EventType.PROJECT_RETRIED:
            raw_target = payload.get("to")
            if not raw_target:
                raise ValueError("PROJECT_RETRIED event missing target 'to' state")
            next_state = ProjectState(raw_target)
            validate_transition(current_state, next_state, allow_retry=True)
            current_state = next_state
            failure_reason = None
            if current_state == ProjectState.WAITING_APPROVAL:
                tests = TestCounts(total=tests.total, passed=0, failed=0)
            last_progress = calculate_progress(
                current_state,
                passed_tests=tests.passed,
                total_tests=tests.total,
                previous_progress=last_progress,
            )

        elif event.type == EventType.PROJECT_DELIVERED:
            validate_transition(current_state, ProjectState.DELIVERED)
            current_state = ProjectState.DELIVERED
            last_progress = 100
            if "repo_url" in payload or "deploy_url" in payload:
                links = ProjectLinks(
                    repo=payload.get("repo_url", links.repo),
                    deploy=payload.get("deploy_url", links.deploy),
                )

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
        links=links,
        workspace_path=workspace_path,
        last_event=last_event,
        updated_at=updated_at,
        failure_reason=failure_reason,
    )
