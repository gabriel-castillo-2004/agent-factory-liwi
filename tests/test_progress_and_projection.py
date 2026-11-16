"""Tests for deterministic progress calculation and status projection (AC-1.6, AC-1.7)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_factory.adapters.file_publisher import FileStatusPublisher
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.domain.events import Event, EventType
from agent_factory.domain.progress import calculate_progress, project_status_from_events
from agent_factory.domain.states import ProjectState


def test_status_reconstructed_identically_after_deleting_projection(tmp_path: Path) -> None:
    """AC-1.6: Deleting the published projection and regenerating from events yields identical status."""
    store = MemoryEventStore()
    publisher = FileStatusPublisher(base_dir=tmp_path)
    project_id = "proj-rebuild-01"
    t0 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    t1 = datetime(2026, 11, 16, 12, 5, tzinfo=UTC)
    t2 = datetime(2026, 11, 16, 12, 10, tzinfo=UTC)
    t3 = datetime(2026, 11, 16, 12, 15, tzinfo=UTC)
    t4 = datetime(2026, 11, 16, 12, 20, tzinfo=UTC)

    events = [
        Event(
            id="evt-1",
            project_id=project_id,
            type=EventType.PROJECT_CREATED,
            payload={
                "name": "Flashcard App",
                "objective": "Spaced repetition flashcards",
                "state": ProjectState.INTERVIEW,
            },
            at=t0,
        ),
        Event(
            id="evt-2",
            project_id=project_id,
            type=EventType.STATE_TRANSITIONED,
            payload={"from": ProjectState.INTERVIEW, "to": ProjectState.DEBATE},
            at=t1,
        ),
        Event(
            id="evt-3",
            project_id=project_id,
            type=EventType.STATE_TRANSITIONED,
            payload={"from": ProjectState.DEBATE, "to": ProjectState.MEDIATION},
            at=t2,
        ),
        Event(
            id="evt-4",
            project_id=project_id,
            type=EventType.PLAN_CREATED,
            payload={
                "plan_hash": "abc123hash",
                "total_tests": 4,
                "to": ProjectState.WAITING_APPROVAL,
            },
            at=t3,
        ),
        Event(
            id="evt-5",
            project_id=project_id,
            type=EventType.APPROVAL_RECORDED,
            payload={"plan_hash": "abc123hash", "to": ProjectState.BUILDING},
            at=t4,
        ),
    ]

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
    """AC-1.7: Progress percentage matches Section 5 table for every state."""
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

    t0 = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
    t1 = datetime(2026, 11, 16, 15, 5, tzinfo=UTC)
    t2 = datetime(2026, 11, 16, 15, 10, tzinfo=UTC)
    events = [
        Event(
            id="e-1",
            project_id="proj-fail",
            type=EventType.PROJECT_CREATED,
            payload={"name": "Test", "objective": "Obj", "state": prior_state},
            at=t0,
        ),
        Event(
            id="e-2",
            project_id="proj-fail",
            type=EventType.TESTS_UPDATED,
            payload={"passed": passed_tests, "failed": total_tests - passed_tests, "total": total_tests},
            at=t1,
        ),
        Event(
            id="e-3",
            project_id="proj-fail",
            type=EventType.PROJECT_FAILED,
            payload={"reason": "Budget exceeded"},
            at=t2,
        ),
    ]
    status = project_status_from_events("proj-fail", events)
    assert status.state == ProjectState.FAILED
    assert status.progress_pct == expected_preserved_pct
