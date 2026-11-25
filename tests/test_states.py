"""Tests for state machine transitions with hand-written oracle (AC-1.1, INV-04, INV-23)."""

from datetime import UTC, datetime
from typing import Any

import pytest

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.domain.events import Event, EventType
from agent_factory.domain.models import ApprovalEvent
from agent_factory.domain.progress import project_status_from_events
from agent_factory.domain.states import (
    IllegalTransition,
    ProjectState,
    can_transition,
    validate_transition,
)
from agent_factory.orchestrator import Orchestrator

# Hand-written oracle for all 20 valid state transitions (INV-23, Section 4)
EXPECTED_VALID_TRANSITIONS: tuple[tuple[ProjectState, ProjectState], ...] = (
    (ProjectState.IDLE, ProjectState.INTERVIEW),
    (ProjectState.INTERVIEW, ProjectState.DEBATE),
    (ProjectState.INTERVIEW, ProjectState.FAILED),
    (ProjectState.DEBATE, ProjectState.MEDIATION),
    (ProjectState.DEBATE, ProjectState.FAILED),
    (ProjectState.MEDIATION, ProjectState.WAITING_APPROVAL),
    (ProjectState.MEDIATION, ProjectState.FAILED),
    (ProjectState.WAITING_APPROVAL, ProjectState.INTERVIEW),
    (ProjectState.WAITING_APPROVAL, ProjectState.DEBATE),
    (ProjectState.WAITING_APPROVAL, ProjectState.BUILDING),
    (ProjectState.WAITING_APPROVAL, ProjectState.FAILED),
    (ProjectState.BUILDING, ProjectState.VERIFYING),
    (ProjectState.BUILDING, ProjectState.FAILED),
    (ProjectState.VERIFYING, ProjectState.BUILDING),
    (ProjectState.VERIFYING, ProjectState.DELIVERED),
    (ProjectState.VERIFYING, ProjectState.FAILED),
    (ProjectState.DELIVERED, ProjectState.CHANGE_REQUEST),
    (ProjectState.DELIVERED, ProjectState.VERIFYING),
    (ProjectState.CHANGE_REQUEST, ProjectState.INTERVIEW),
    (ProjectState.CHANGE_REQUEST, ProjectState.FAILED),
)

# Hand-written oracle for all 80 illegal state transitions in the 10x10 matrix (INV-23)
EXPECTED_ILLEGAL_TRANSITIONS: tuple[tuple[ProjectState, ProjectState], ...] = (
    # From IDLE (9 illegal)
    (ProjectState.IDLE, ProjectState.IDLE),
    (ProjectState.IDLE, ProjectState.DEBATE),
    (ProjectState.IDLE, ProjectState.MEDIATION),
    (ProjectState.IDLE, ProjectState.WAITING_APPROVAL),
    (ProjectState.IDLE, ProjectState.BUILDING),
    (ProjectState.IDLE, ProjectState.VERIFYING),
    (ProjectState.IDLE, ProjectState.DELIVERED),
    (ProjectState.IDLE, ProjectState.FAILED),
    (ProjectState.IDLE, ProjectState.CHANGE_REQUEST),
    # From INTERVIEW (8 illegal)
    (ProjectState.INTERVIEW, ProjectState.IDLE),
    (ProjectState.INTERVIEW, ProjectState.INTERVIEW),
    (ProjectState.INTERVIEW, ProjectState.MEDIATION),
    (ProjectState.INTERVIEW, ProjectState.WAITING_APPROVAL),
    (ProjectState.INTERVIEW, ProjectState.BUILDING),
    (ProjectState.INTERVIEW, ProjectState.VERIFYING),
    (ProjectState.INTERVIEW, ProjectState.DELIVERED),
    (ProjectState.INTERVIEW, ProjectState.CHANGE_REQUEST),
    # From DEBATE (8 illegal)
    (ProjectState.DEBATE, ProjectState.IDLE),
    (ProjectState.DEBATE, ProjectState.INTERVIEW),
    (ProjectState.DEBATE, ProjectState.DEBATE),
    (ProjectState.DEBATE, ProjectState.WAITING_APPROVAL),
    (ProjectState.DEBATE, ProjectState.BUILDING),
    (ProjectState.DEBATE, ProjectState.VERIFYING),
    (ProjectState.DEBATE, ProjectState.DELIVERED),
    (ProjectState.DEBATE, ProjectState.CHANGE_REQUEST),
    # From MEDIATION (8 illegal)
    (ProjectState.MEDIATION, ProjectState.IDLE),
    (ProjectState.MEDIATION, ProjectState.INTERVIEW),
    (ProjectState.MEDIATION, ProjectState.DEBATE),
    (ProjectState.MEDIATION, ProjectState.MEDIATION),
    (ProjectState.MEDIATION, ProjectState.BUILDING),
    (ProjectState.MEDIATION, ProjectState.VERIFYING),
    (ProjectState.MEDIATION, ProjectState.DELIVERED),
    (ProjectState.MEDIATION, ProjectState.CHANGE_REQUEST),
    # From WAITING_APPROVAL (6 illegal)
    (ProjectState.WAITING_APPROVAL, ProjectState.IDLE),
    (ProjectState.WAITING_APPROVAL, ProjectState.MEDIATION),
    (ProjectState.WAITING_APPROVAL, ProjectState.WAITING_APPROVAL),
    (ProjectState.WAITING_APPROVAL, ProjectState.VERIFYING),
    (ProjectState.WAITING_APPROVAL, ProjectState.DELIVERED),
    (ProjectState.WAITING_APPROVAL, ProjectState.CHANGE_REQUEST),
    # From BUILDING (8 illegal)
    (ProjectState.BUILDING, ProjectState.IDLE),
    (ProjectState.BUILDING, ProjectState.INTERVIEW),
    (ProjectState.BUILDING, ProjectState.DEBATE),
    (ProjectState.BUILDING, ProjectState.MEDIATION),
    (ProjectState.BUILDING, ProjectState.WAITING_APPROVAL),
    (ProjectState.BUILDING, ProjectState.BUILDING),
    (ProjectState.BUILDING, ProjectState.DELIVERED),
    (ProjectState.BUILDING, ProjectState.CHANGE_REQUEST),
    # From VERIFYING (7 illegal)
    (ProjectState.VERIFYING, ProjectState.IDLE),
    (ProjectState.VERIFYING, ProjectState.INTERVIEW),
    (ProjectState.VERIFYING, ProjectState.DEBATE),
    (ProjectState.VERIFYING, ProjectState.MEDIATION),
    (ProjectState.VERIFYING, ProjectState.WAITING_APPROVAL),
    (ProjectState.VERIFYING, ProjectState.VERIFYING),
    (ProjectState.VERIFYING, ProjectState.CHANGE_REQUEST),
    # From DELIVERED (8 illegal)
    (ProjectState.DELIVERED, ProjectState.IDLE),
    (ProjectState.DELIVERED, ProjectState.INTERVIEW),
    (ProjectState.DELIVERED, ProjectState.DEBATE),
    (ProjectState.DELIVERED, ProjectState.MEDIATION),
    (ProjectState.DELIVERED, ProjectState.WAITING_APPROVAL),
    (ProjectState.DELIVERED, ProjectState.BUILDING),
    (ProjectState.DELIVERED, ProjectState.DELIVERED),
    (ProjectState.DELIVERED, ProjectState.FAILED),
    # From FAILED (10 illegal - terminal state)
    (ProjectState.FAILED, ProjectState.IDLE),
    (ProjectState.FAILED, ProjectState.INTERVIEW),
    (ProjectState.FAILED, ProjectState.DEBATE),
    (ProjectState.FAILED, ProjectState.MEDIATION),
    (ProjectState.FAILED, ProjectState.WAITING_APPROVAL),
    (ProjectState.FAILED, ProjectState.BUILDING),
    (ProjectState.FAILED, ProjectState.VERIFYING),
    (ProjectState.FAILED, ProjectState.DELIVERED),
    (ProjectState.FAILED, ProjectState.FAILED),
    (ProjectState.FAILED, ProjectState.CHANGE_REQUEST),
    # From CHANGE_REQUEST (8 illegal)
    (ProjectState.CHANGE_REQUEST, ProjectState.IDLE),
    (ProjectState.CHANGE_REQUEST, ProjectState.DEBATE),
    (ProjectState.CHANGE_REQUEST, ProjectState.MEDIATION),
    (ProjectState.CHANGE_REQUEST, ProjectState.WAITING_APPROVAL),
    (ProjectState.CHANGE_REQUEST, ProjectState.BUILDING),
    (ProjectState.CHANGE_REQUEST, ProjectState.VERIFYING),
    (ProjectState.CHANGE_REQUEST, ProjectState.DELIVERED),
    (ProjectState.CHANGE_REQUEST, ProjectState.CHANGE_REQUEST),
)


def _pipeline_responses() -> list[dict[str, Any]]:
    return [
        {
            "spec": {
                "objective": "Static unit converter",
                "users": ["Gabriel"],
                "features": ["Convert length and weight"],
                "business_rules": ["Round to 4 decimal places"],
                "edge_cases": ["Negative inputs"],
                "constraints": ["Zero backend"],
                "success_criteria": ["Instant conversion"],
                "assumptions": [],
            },
            "questions": [],
        },
        {
            "approach": "Pure client-side conversion functions",
            "components": ["converter.ts", "ui.ts"],
            "tradeoffs": ["Hardcoded conversion table"],
        },
        {
            "critique": ["Floating point precision issues"],
            "robust_proposal": "Scaled integer math and input validation",
            "architectural_safeguards": ["Strict numeric parser"],
        },
        {
            "summary": "Unit converter static web application",
            "rationale": "Simple client-side architecture with numeric validation",
            "template_id": "static-app",
            "files": [
                {
                    "path": "src/converter.ts",
                    "purpose": "Conversion formulas",
                    "contract": "Exports convert(value: number, from: string, to: string): number",
                    "depends_on": [],
                }
            ],
            "acceptance_tests": [
                {
                    "id": "AT-01",
                    "description": "Converts meters to kilometers accurately",
                    "type": "unit",
                }
            ],
            "risks": ["None"],
            "deployment_target": "github-pages",
        },
    ]


@pytest.mark.parametrize(("source", "target"), EXPECTED_VALID_TRANSITIONS)
def test_valid_transitions_succeed(source: ProjectState, target: ProjectState) -> None:
    """AC-1.1 / F-09: Every hand-written valid transition succeeds and returns target."""
    assert can_transition(source, target) is True
    result = validate_transition(source, target)
    assert result == target


@pytest.mark.parametrize(("source", "target"), EXPECTED_ILLEGAL_TRANSITIONS)
def test_illegal_transitions_raise_illegal_transition(
    source: ProjectState,
    target: ProjectState,
) -> None:
    """AC-1.1 / F-09 / M1: Every hand-written illegal transition raises IllegalTransition."""
    assert can_transition(source, target) is False
    with pytest.raises(IllegalTransition) as exc_info:
        validate_transition(source, target)
    assert exc_info.value.current == source
    assert exc_info.value.target == target


def test_orchestrator_run_debate_rejected_when_project_in_building_state() -> None:
    """AC-1.1 / F-02 / F-03: run_debate must reject execution when project is already in BUILDING."""
    store = MemoryEventStore()
    responses = _pipeline_responses()
    fake_llm = FakeLLM(responses=[*responses, *responses[1:]])
    orchestrator = Orchestrator(store=store, llm=fake_llm)
    project_id = "proj-origin-building"

    orchestrator.create_project(project_id=project_id, name="Origin Test", objective="Converter")
    orchestrator.handle_interview_message(project_id, "evt-1", "Complete converter spec")
    waiting = orchestrator.run_debate(project_id)
    assert waiting.plan_hash is not None

    building = orchestrator.submit_approval(
        project_id,
        ApprovalEvent(
            plan_hash=waiting.plan_hash,
            timestamp=datetime(2026, 11, 16, 15, 0, tzinfo=UTC),
            decision="approved",
        ),
    )
    assert building.state == ProjectState.BUILDING

    with pytest.raises(IllegalTransition):
        orchestrator.run_debate(project_id)

    assert orchestrator.get_status(project_id).state == ProjectState.BUILDING


def test_orchestrator_run_debate_rejected_when_project_in_failed_state() -> None:
    """AC-1.1 / F-02 / F-03: A FAILED project is terminal and cannot be resurrected by run_debate."""
    store = MemoryEventStore()
    fake_llm = FakeLLM(responses=_pipeline_responses()[1:])
    orchestrator = Orchestrator(store=store, llm=fake_llm)
    project_id = "proj-origin-failed"

    orchestrator.create_project(project_id=project_id, name="Failed Test", objective="Converter")
    for _ in range(7):
        orchestrator.record_state_call(project_id, ProjectState.INTERVIEW)

    assert orchestrator.get_status(project_id).state == ProjectState.FAILED

    with pytest.raises(IllegalTransition):
        orchestrator.run_debate(project_id)

    assert orchestrator.get_status(project_id).state == ProjectState.FAILED


def test_orchestrator_run_debate_rejected_when_spec_is_incomplete() -> None:
    """AC-1.1 / F-03: run_debate must reject execution if Spec checklist is incomplete."""
    store = MemoryEventStore()
    fake_llm = FakeLLM(responses=_pipeline_responses()[1:])
    orchestrator = Orchestrator(store=store, llm=fake_llm)
    project_id = "proj-incomplete-spec"

    orchestrator.create_project(project_id=project_id, name="Empty Spec", objective="")

    with pytest.raises((IllegalTransition, ValueError)):
        orchestrator.run_debate(project_id)

    assert orchestrator.get_status(project_id).state == ProjectState.INTERVIEW


def test_orchestrator_handle_interview_message_rejected_when_not_in_interview() -> None:
    """AC-1.1 / F-02 / F-18: handle_interview_message rejects projects in BUILDING or FAILED."""
    store = MemoryEventStore()
    responses = _pipeline_responses()
    fake_llm = FakeLLM(responses=[*responses, responses[0]])
    orchestrator = Orchestrator(store=store, llm=fake_llm)
    project_id = "proj-no-interview-in-building"

    orchestrator.create_project(project_id=project_id, name="Test", objective="Converter")
    orchestrator.handle_interview_message(project_id, "evt-1", "Complete spec")
    waiting = orchestrator.run_debate(project_id)
    assert waiting.plan_hash is not None

    orchestrator.submit_approval(
        project_id,
        ApprovalEvent(
            plan_hash=waiting.plan_hash,
            timestamp=datetime(2026, 11, 16, 15, 0, tzinfo=UTC),
            decision="approved",
        ),
    )

    with pytest.raises(IllegalTransition):
        orchestrator.handle_interview_message(project_id, "evt-2", "Late interview message")

    assert orchestrator.get_status(project_id).state == ProjectState.BUILDING


def test_orchestrator_create_project_twice_with_same_id_fails_without_corrupting_log() -> None:
    """N-03 (INV-04, INV-11): Calling create_project twice with the same project_id fails without poisoning the log."""
    store = MemoryEventStore()
    orchestrator = Orchestrator(store=store, llm=FakeLLM())
    project_id = "proj-dup-create"

    first_status = orchestrator.create_project(
        project_id=project_id,
        name="Original Name",
        objective="Original Objective",
    )
    assert first_status.state == ProjectState.INTERVIEW
    assert len(store.get_events(project_id)) == 1

    with pytest.raises((IllegalTransition, ValueError)):
        orchestrator.create_project(
            project_id=project_id,
            name="Duplicate Name",
            objective="Duplicate Objective",
        )

    assert len(store.get_events(project_id)) == 1
    status_after = orchestrator.get_status(project_id)
    assert status_after.state == ProjectState.INTERVIEW
    assert status_after.name == "Original Name"
    assert status_after.objective == "Original Objective"


def test_orchestrator_record_state_call_when_already_failed_is_rejected_without_duplicating_failed_event() -> None:
    """N-04 (INV-04, INV-11): Calling record_state_call on a FAILED project raises IllegalTransition and never duplicates PROJECT_FAILED."""
    store = MemoryEventStore()
    orchestrator = Orchestrator(store=store, llm=FakeLLM())
    project_id = "proj-record-after-failed"

    orchestrator.create_project(project_id=project_id, name="Failed Guard", objective="Converter")
    for _ in range(7):
        orchestrator.record_state_call(project_id, ProjectState.INTERVIEW)

    assert orchestrator.get_status(project_id).state == ProjectState.FAILED
    failed_events_before = [
        e for e in store.get_events(project_id) if e.type == EventType.PROJECT_FAILED
    ]
    assert len(failed_events_before) == 1

    with pytest.raises(IllegalTransition):
        orchestrator.record_state_call(project_id, ProjectState.INTERVIEW)

    with pytest.raises(IllegalTransition):
        orchestrator.record_state_call(project_id, ProjectState.BUILDING)

    failed_events_after = [
        e for e in store.get_events(project_id) if e.type == EventType.PROJECT_FAILED
    ]
    assert len(failed_events_after) == 1
    assert orchestrator.get_status(project_id).state == ProjectState.FAILED


def test_delivered_to_verifying_reverification_transition_and_event_projection() -> None:
    """INV-04 / INV-11: validate_transition and project_status_from_events allow DELIVERED -> VERIFYING for post-delivery re-verification."""
    assert validate_transition(ProjectState.DELIVERED, ProjectState.VERIFYING) == ProjectState.VERIFYING

    project_id = "proj-delivered-reverify"
    base_time = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
    events: list[Event] = [
        Event(
            id="evt-1",
            project_id=project_id,
            type=EventType.PROJECT_CREATED,
            payload={
                "name": "Reverify App",
                "objective": "Verify post-delivery transition",
                "state": ProjectState.INTERVIEW,
            },
            at=base_time,
        ),
        Event(
            id="evt-2",
            project_id=project_id,
            type=EventType.STATE_TRANSITIONED,
            payload={"from": ProjectState.INTERVIEW, "to": ProjectState.DEBATE},
            at=datetime(2026, 11, 25, 15, 1, tzinfo=UTC),
        ),
        Event(
            id="evt-3",
            project_id=project_id,
            type=EventType.STATE_TRANSITIONED,
            payload={"from": ProjectState.DEBATE, "to": ProjectState.MEDIATION},
            at=datetime(2026, 11, 25, 15, 2, tzinfo=UTC),
        ),
        Event(
            id="evt-4",
            project_id=project_id,
            type=EventType.PLAN_CREATED,
            payload={
                "plan_hash": "hash-reverify-01",
                "total_tests": 2,
                "to": ProjectState.WAITING_APPROVAL,
            },
            at=datetime(2026, 11, 25, 15, 3, tzinfo=UTC),
        ),
        Event(
            id="evt-5",
            project_id=project_id,
            type=EventType.APPROVAL_RECORDED,
            payload={
                "plan_hash": "hash-reverify-01",
                "decision": "approved",
                "to": ProjectState.BUILDING,
            },
            at=datetime(2026, 11, 25, 15, 4, tzinfo=UTC),
        ),
        Event(
            id="evt-6",
            project_id=project_id,
            type=EventType.STATE_TRANSITIONED,
            payload={"from": ProjectState.BUILDING, "to": ProjectState.VERIFYING},
            at=datetime(2026, 11, 25, 15, 5, tzinfo=UTC),
        ),
        Event(
            id="evt-7",
            project_id=project_id,
            type=EventType.PROJECT_DELIVERED,
            payload={"repo_url": "https://github.com/org/reverify", "commit_sha": "abc1234"},
            at=datetime(2026, 11, 25, 15, 6, tzinfo=UTC),
        ),
        Event(
            id="evt-8",
            project_id=project_id,
            type=EventType.STATE_TRANSITIONED,
            payload={"from": ProjectState.DELIVERED, "to": ProjectState.VERIFYING},
            at=datetime(2026, 11, 25, 15, 7, tzinfo=UTC),
        ),
    ]

    status_verifying = project_status_from_events(project_id, events)
    assert status_verifying.state == ProjectState.VERIFYING
    assert status_verifying.progress_pct == 95
    assert status_verifying.plan_hash == "hash-reverify-01"

    events.append(
        Event(
            id="evt-9",
            project_id=project_id,
            type=EventType.PROJECT_DELIVERED,
            payload={"repo_url": "https://github.com/org/reverify", "commit_sha": "def5678"},
            at=datetime(2026, 11, 25, 15, 8, tzinfo=UTC),
        )
    )
    status_redelivered = project_status_from_events(project_id, events)
    assert status_redelivered.state == ProjectState.DELIVERED
    assert status_redelivered.progress_pct == 100
