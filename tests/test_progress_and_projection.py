"""Tests for deterministic progress calculation and status projection (AC-1.6, AC-1.7, F-04, F-10)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_factory.adapters.file_publisher import FileStatusPublisher
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.domain.events import Event, EventType
from agent_factory.domain.progress import calculate_progress, project_status_from_events
from agent_factory.domain.states import IllegalTransition, ProjectState


def _events_up_to_state(
    project_id: str,
    target_state: ProjectState,
    passed_tests: int = 0,
    total_tests: int = 0,
) -> list[Event]:
    """Build a valid canonical sequence of events reaching target_state."""
    base_time = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    events: list[Event] = [
        Event(
            id="evt-1",
            project_id=project_id,
            type=EventType.PROJECT_CREATED,
            payload={
                "name": "Flashcard App",
                "objective": "Spaced repetition flashcards",
                "state": ProjectState.INTERVIEW,
            },
            at=base_time,
        )
    ]
    if target_state == ProjectState.INTERVIEW:
        return events

    events.append(
        Event(
            id="evt-2",
            project_id=project_id,
            type=EventType.STATE_TRANSITIONED,
            payload={"from": ProjectState.INTERVIEW, "to": ProjectState.DEBATE},
            at=datetime(2026, 11, 16, 12, 5, tzinfo=UTC),
        )
    )
    if target_state == ProjectState.DEBATE:
        return events

    events.append(
        Event(
            id="evt-3",
            project_id=project_id,
            type=EventType.STATE_TRANSITIONED,
            payload={"from": ProjectState.DEBATE, "to": ProjectState.MEDIATION},
            at=datetime(2026, 11, 16, 12, 10, tzinfo=UTC),
        )
    )
    if target_state == ProjectState.MEDIATION:
        return events

    events.append(
        Event(
            id="evt-4",
            project_id=project_id,
            type=EventType.PLAN_CREATED,
            payload={
                "plan_hash": "abc123hash",
                "total_tests": total_tests or 4,
                "to": ProjectState.WAITING_APPROVAL,
            },
            at=datetime(2026, 11, 16, 12, 15, tzinfo=UTC),
        )
    )
    if target_state == ProjectState.WAITING_APPROVAL:
        return events

    events.append(
        Event(
            id="evt-5",
            project_id=project_id,
            type=EventType.APPROVAL_RECORDED,
            payload={
                "plan_hash": "abc123hash",
                "decision": "approved",
                "to": ProjectState.BUILDING,
            },
            at=datetime(2026, 11, 16, 12, 20, tzinfo=UTC),
        )
    )
    if total_tests > 0:
        events.append(
            Event(
                id="evt-6",
                project_id=project_id,
                type=EventType.TESTS_UPDATED,
                payload={
                    "passed": passed_tests,
                    "failed": total_tests - passed_tests,
                    "total": total_tests,
                },
                at=datetime(2026, 11, 16, 12, 25, tzinfo=UTC),
            )
        )
    if target_state == ProjectState.BUILDING:
        return events

    events.append(
        Event(
            id="evt-7",
            project_id=project_id,
            type=EventType.STATE_TRANSITIONED,
            payload={"from": ProjectState.BUILDING, "to": ProjectState.VERIFYING},
            at=datetime(2026, 11, 16, 12, 30, tzinfo=UTC),
        )
    )
    return events


def test_status_reconstructed_identically_after_deleting_projection(tmp_path: Path) -> None:
    """AC-1.6 / F-10 / M5: Regenerated status matches both original and hand-written expected values."""
    store = MemoryEventStore()
    publisher = FileStatusPublisher(base_dir=tmp_path)
    project_id = "proj-rebuild-01"
    t_final = datetime(2026, 11, 16, 12, 40, tzinfo=UTC)

    events = _events_up_to_state(
        project_id=project_id,
        target_state=ProjectState.BUILDING,
        passed_tests=2,
        total_tests=4,
    )
    events.extend(
        [
            Event(
                id="evt-inv-1",
                project_id=project_id,
                type=EventType.AGENT_INVOKED,
                payload={"state": "INTERVIEW"},
                at=datetime(2026, 11, 16, 12, 31, tzinfo=UTC),
            ),
            Event(
                id="evt-inv-2",
                project_id=project_id,
                type=EventType.AGENT_INVOKED,
                payload={"state": "BUILDING"},
                at=datetime(2026, 11, 16, 12, 35, tzinfo=UTC),
            ),
            Event(
                id="evt-ci-1",
                project_id=project_id,
                type=EventType.CI_ROUND_COMPLETED,
                payload={"ci_rounds": 1},
                at=t_final,
            ),
        ]
    )

    for event in events:
        store.append(event)

    initial_status = project_status_from_events(project_id, store.get_events(project_id))
    publisher.publish(initial_status)

    loaded_before_delete = publisher.load(project_id)
    assert loaded_before_delete == initial_status

    publisher.delete(project_id)
    assert publisher.load(project_id) is None

    regenerated_status = publisher.rebuild_and_publish(project_id, store)
    loaded_after_rebuild = publisher.load(project_id)

    assert regenerated_status == initial_status
    assert loaded_after_rebuild == initial_status

    # Hand-written expected values (INV-23, F-10, M5)
    assert regenerated_status.project_id == "proj-rebuild-01"
    assert regenerated_status.name == "Flashcard App"
    assert regenerated_status.objective == "Spaced repetition flashcards"
    assert regenerated_status.state == ProjectState.BUILDING
    assert regenerated_status.phase == "BUILDING"
    assert regenerated_status.progress_pct == 62
    assert regenerated_status.plan_hash == "abc123hash"
    assert regenerated_status.tests.total == 4
    assert regenerated_status.tests.passed == 2
    assert regenerated_status.tests.failed == 2
    assert regenerated_status.agent_invocations == 2
    assert regenerated_status.ci_rounds == 1
    assert regenerated_status.last_event == "evt-ci-1"
    assert regenerated_status.updated_at == t_final


def test_projection_rejects_illegal_state_transition_in_event_log() -> None:
    """AC-1.6 / F-04 (INV-04, INV-11): Replaying an illegal state transition raises IllegalTransition."""
    project_id = "proj-illegal-replay"
    events = [
        Event(
            id="evt-1",
            project_id=project_id,
            type=EventType.PROJECT_CREATED,
            payload={"name": "App", "objective": "Obj", "state": ProjectState.INTERVIEW},
            at=datetime(2026, 11, 16, 12, 0, tzinfo=UTC),
        ),
        Event(
            id="evt-2-illegal",
            project_id=project_id,
            type=EventType.STATE_TRANSITIONED,
            payload={"from": ProjectState.INTERVIEW, "to": ProjectState.DELIVERED},
            at=datetime(2026, 11, 16, 12, 5, tzinfo=UTC),
        ),
    ]

    with pytest.raises(IllegalTransition):
        project_status_from_events(project_id, events)


@pytest.mark.parametrize(
    "bad_approval_event",
    [
        Event(
            id="evt-bad-1",
            project_id="proj-bad-approval",
            type=EventType.STATE_TRANSITIONED,
            payload={"from": ProjectState.WAITING_APPROVAL, "to": ProjectState.BUILDING},
            at=datetime(2026, 11, 16, 12, 25, tzinfo=UTC),
        ),
        Event(
            id="evt-bad-2",
            project_id="proj-bad-approval",
            type=EventType.APPROVAL_RECORDED,
            payload={
                "plan_hash": "abc123hash",
                "decision": "rejected",
                "to": ProjectState.BUILDING,
            },
            at=datetime(2026, 11, 16, 12, 25, tzinfo=UTC),
        ),
        Event(
            id="evt-bad-3",
            project_id="proj-bad-approval",
            type=EventType.APPROVAL_RECORDED,
            payload={
                "plan_hash": "tampered-wrong-hash",
                "decision": "approved",
                "to": ProjectState.BUILDING,
            },
            at=datetime(2026, 11, 16, 12, 25, tzinfo=UTC),
        ),
    ],
)
def test_projection_rejects_building_without_matching_approved_decision(
    bad_approval_event: Event,
) -> None:
    """AC-1.6 / F-04 (INV-05, INV-11): Projection rejects entering BUILDING without valid approval."""
    project_id = "proj-bad-approval"
    events = _events_up_to_state(
        project_id=project_id,
        target_state=ProjectState.WAITING_APPROVAL,
        total_tests=4,
    )
    events.append(bad_approval_event)

    with pytest.raises((IllegalTransition, ValueError)):
        project_status_from_events(project_id, events)


@pytest.mark.parametrize(
    ("state", "passed_tests", "total_tests", "expected_pct"),
    [
        (ProjectState.IDLE, 0, 0, 0),
        (ProjectState.INTERVIEW, 0, 0, 10),
        (ProjectState.DEBATE, 0, 0, 25),
        (ProjectState.MEDIATION, 0, 0, 30),
        (ProjectState.WAITING_APPROVAL, 0, 0, 35),
        (ProjectState.BUILDING, 0, 4, 35),
        (ProjectState.BUILDING, 2, 4, 62),
        (ProjectState.BUILDING, 4, 4, 90),
        (ProjectState.VERIFYING, 4, 4, 95),
        (ProjectState.DELIVERED, 4, 4, 100),
    ],
)
def test_progress_calculation_matches_section_5_table_per_state(
    state: ProjectState,
    passed_tests: int,
    total_tests: int,
    expected_pct: int,
) -> None:
    """AC-1.7 / M10: Progress percentage matches Section 5 table for every state."""
    pct = calculate_progress(
        state,
        passed_tests=passed_tests,
        total_tests=total_tests,
    )
    assert pct == expected_pct


@pytest.mark.parametrize(
    ("prior_state", "passed_tests", "total_tests", "expected_preserved_pct"),
    [
        (ProjectState.INTERVIEW, 0, 0, 10),
        (ProjectState.DEBATE, 0, 0, 25),
        (ProjectState.MEDIATION, 0, 0, 30),
        (ProjectState.WAITING_APPROVAL, 0, 0, 35),
        (ProjectState.BUILDING, 2, 4, 62),
        (ProjectState.VERIFYING, 4, 4, 95),
    ],
)
def test_failed_state_preserves_last_progress_value(
    prior_state: ProjectState,
    passed_tests: int,
    total_tests: int,
    expected_preserved_pct: int,
) -> None:
    """AC-1.7: Transitioning to FAILED preserves the last progress percentage."""
    prior_pct = calculate_progress(
        prior_state,
        passed_tests=passed_tests,
        total_tests=total_tests,
    )
    assert prior_pct == expected_preserved_pct

    failed_pct = calculate_progress(
        ProjectState.FAILED,
        passed_tests=passed_tests,
        total_tests=total_tests,
        previous_progress=prior_pct,
    )
    assert failed_pct == expected_preserved_pct

    events = _events_up_to_state(
        project_id="proj-fail",
        target_state=prior_state,
        passed_tests=passed_tests,
        total_tests=total_tests,
    )
    events.append(
        Event(
            id="evt-fail-final",
            project_id="proj-fail",
            type=EventType.PROJECT_FAILED,
            payload={"reason": "Budget exceeded"},
            at=datetime(2026, 11, 16, 15, 10, tzinfo=UTC),
        )
    )
    status = project_status_from_events("proj-fail", events)
    assert status.state == ProjectState.FAILED
    assert status.progress_pct == expected_preserved_pct


@pytest.mark.parametrize(
    "invalid_plan_state",
    [
        ProjectState.INTERVIEW,
        ProjectState.BUILDING,
        ProjectState.VERIFYING,
    ],
)
def test_projection_rejects_plan_created_outside_debate_or_mediation(
    invalid_plan_state: ProjectState,
) -> None:
    """N-07 (INV-11, INV-05): project_status_from_events rejects PLAN_CREATED outside DEBATE/MEDIATION."""
    project_id = f"proj-bad-plan-{invalid_plan_state.value.lower()}"
    events = _events_up_to_state(
        project_id=project_id,
        target_state=invalid_plan_state,
        passed_tests=2,
        total_tests=4,
    )
    events.append(
        Event(
            id="evt-rogue-plan",
            project_id=project_id,
            type=EventType.PLAN_CREATED,
            payload={"plan_hash": "rogue-tampered-hash", "total_tests": 1},
            at=datetime(2026, 11, 16, 16, 0, tzinfo=UTC),
        )
    )

    with pytest.raises((IllegalTransition, ValueError)):
        project_status_from_events(project_id, events)


@pytest.mark.parametrize(
    ("prior_state", "rogue_target"),
    [
        (ProjectState.WAITING_APPROVAL, ProjectState.DEBATE),
        (ProjectState.VERIFYING, ProjectState.DELIVERED),
    ],
)
def test_projection_rejects_approval_recorded_with_target_other_than_building(
    prior_state: ProjectState,
    rogue_target: ProjectState,
) -> None:
    """N-07 (INV-05, INV-11): APPROVAL_RECORDED must only target BUILDING from WAITING_APPROVAL."""
    project_id = f"proj-bad-app-target-{rogue_target.value.lower()}"
    events = _events_up_to_state(
        project_id=project_id,
        target_state=prior_state,
        passed_tests=4,
        total_tests=4,
    )
    events.append(
        Event(
            id="evt-rogue-approval-target",
            project_id=project_id,
            type=EventType.APPROVAL_RECORDED,
            payload={
                "plan_hash": "abc123hash",
                "decision": "rejected",
                "to": rogue_target,
            },
            at=datetime(2026, 11, 16, 16, 5, tzinfo=UTC),
        )
    )

    with pytest.raises((IllegalTransition, ValueError)):
        project_status_from_events(project_id, events)


@pytest.mark.parametrize(
    "premature_event",
    [
        Event(
            id="evt-pre-inv",
            project_id="proj-pre-create",
            type=EventType.AGENT_INVOKED,
            payload={"state": "INTERVIEW"},
            at=datetime(2026, 11, 16, 11, 0, tzinfo=UTC),
        ),
        Event(
            id="evt-pre-tests",
            project_id="proj-pre-create",
            type=EventType.TESTS_UPDATED,
            payload={"passed": 1, "failed": 0, "total": 1},
            at=datetime(2026, 11, 16, 11, 0, tzinfo=UTC),
        ),
        Event(
            id="evt-pre-ci",
            project_id="proj-pre-create",
            type=EventType.CI_ROUND_COMPLETED,
            payload={"ci_rounds": 1},
            at=datetime(2026, 11, 16, 11, 0, tzinfo=UTC),
        ),
        Event(
            id="evt-pre-spec",
            project_id="proj-pre-create",
            type=EventType.SPEC_UPDATED,
            payload={"spec": {"objective": "Premature"}},
            at=datetime(2026, 11, 16, 11, 0, tzinfo=UTC),
        ),
        Event(
            id="evt-pre-fail",
            project_id="proj-pre-create",
            type=EventType.PROJECT_FAILED,
            payload={"reason": "Premature fail from IDLE"},
            at=datetime(2026, 11, 16, 11, 0, tzinfo=UTC),
        ),
    ],
)
def test_projection_rejects_events_before_project_created_and_illegal_failed_transitions(
    premature_event: Event,
) -> None:
    """N-07 / X9 (INV-04, INV-11): Reject events prior to PROJECT_CREATED and illegal transitions to FAILED."""
    with pytest.raises((IllegalTransition, ValueError)):
        project_status_from_events("proj-pre-create", [premature_event])

    # Also verify duplicate PROJECT_FAILED (FAILED -> FAILED) is rejected (kills X9)
    fail_events = _events_up_to_state("proj-dup-fail", ProjectState.INTERVIEW)
    fail_events.append(
        Event(
            id="evt-f1",
            project_id="proj-dup-fail",
            type=EventType.PROJECT_FAILED,
            payload={"reason": "First failure"},
            at=datetime(2026, 11, 16, 13, 0, tzinfo=UTC),
        )
    )
    fail_events.append(
        Event(
            id="evt-f2",
            project_id="proj-dup-fail",
            type=EventType.PROJECT_FAILED,
            payload={"reason": "Second failure"},
            at=datetime(2026, 11, 16, 13, 1, tzinfo=UTC),
        )
    )
    with pytest.raises(IllegalTransition):
        project_status_from_events("proj-dup-fail", fail_events)


def test_projection_accepts_legitimate_verifying_to_building_transition() -> None:
    """N-07 (Section 4, INV-04): Projection accepts legitimate VERIFYING -> BUILDING self-correction transition."""
    project_id = "proj-verify-to-building"
    events = _events_up_to_state(
        project_id=project_id,
        target_state=ProjectState.VERIFYING,
        passed_tests=2,
        total_tests=4,
    )
    events.append(
        Event(
            id="evt-reenter-building",
            project_id=project_id,
            type=EventType.STATE_TRANSITIONED,
            payload={"from": ProjectState.VERIFYING, "to": ProjectState.BUILDING},
            at=datetime(2026, 11, 16, 12, 35, tzinfo=UTC),
        )
    )

    status = project_status_from_events(project_id, events)
    assert status.state == ProjectState.BUILDING
    assert status.progress_pct == 62
    assert status.plan_hash == "abc123hash"
