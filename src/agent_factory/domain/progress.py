"""Deterministic progress calculation and event-log status projection (INV-11, INV-12)."""

from collections.abc import Sequence

from agent_factory.domain.events import Event
from agent_factory.domain.models import ProjectStatus
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
    raise NotImplementedError


def project_status_from_events(project_id: str, events: Sequence[Event]) -> ProjectStatus:
    """Rebuild ProjectStatus deterministically from the append-only event log (INV-11)."""
    raise NotImplementedError
