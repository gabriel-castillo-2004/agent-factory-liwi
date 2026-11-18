"""Explicit state machine definitions and transition table (INV-04, Section 4)."""

from enum import StrEnum


class ProjectState(StrEnum):
    """Valid states for a project in Agent Factory."""

    IDLE = "IDLE"
    INTERVIEW = "INTERVIEW"
    DEBATE = "DEBATE"
    MEDIATION = "MEDIATION"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    BUILDING = "BUILDING"
    VERIFYING = "VERIFYING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    CHANGE_REQUEST = "CHANGE_REQUEST"


class IllegalTransition(Exception):
    """Raised when attempting a state transition not in ALLOWED_TRANSITIONS (INV-04)."""

    def __init__(self, current: ProjectState, target: ProjectState) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Illegal transition from {current} to {target}")


ALLOWED_TRANSITIONS: dict[ProjectState, frozenset[ProjectState]] = {
    ProjectState.IDLE: frozenset({ProjectState.INTERVIEW}),
    ProjectState.INTERVIEW: frozenset({ProjectState.DEBATE, ProjectState.FAILED}),
    ProjectState.DEBATE: frozenset({ProjectState.MEDIATION, ProjectState.FAILED}),
    ProjectState.MEDIATION: frozenset({ProjectState.WAITING_APPROVAL, ProjectState.FAILED}),
    ProjectState.WAITING_APPROVAL: frozenset(
        {
            ProjectState.INTERVIEW,
            ProjectState.DEBATE,
            ProjectState.BUILDING,
            ProjectState.FAILED,
        }
    ),
    ProjectState.BUILDING: frozenset({ProjectState.VERIFYING, ProjectState.FAILED}),
    ProjectState.VERIFYING: frozenset(
        {
            ProjectState.BUILDING,
            ProjectState.DELIVERED,
            ProjectState.FAILED,
        }
    ),
    ProjectState.DELIVERED: frozenset({ProjectState.CHANGE_REQUEST}),
    ProjectState.CHANGE_REQUEST: frozenset({ProjectState.INTERVIEW, ProjectState.FAILED}),
    ProjectState.FAILED: frozenset(),
}


RETRYABLE_SOURCE_STATES: frozenset[ProjectState] = frozenset(
    {
        ProjectState.FAILED,
        ProjectState.BUILDING,
        ProjectState.VERIFYING,
        ProjectState.DEBATE,
        ProjectState.MEDIATION,
    }
)

RETRYABLE_TARGET_STATES: frozenset[ProjectState] = frozenset(
    {
        ProjectState.INTERVIEW,
        ProjectState.DEBATE,
        ProjectState.MEDIATION,
        ProjectState.WAITING_APPROVAL,
        ProjectState.BUILDING,
        ProjectState.VERIFYING,
    }
)


def can_transition(current: ProjectState, target: ProjectState) -> bool:
    """Return True if transitioning from current to target is allowed."""
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    return target in allowed


def validate_transition(
    current: ProjectState,
    target: ProjectState,
    *,
    allow_retry: bool = False,
) -> ProjectState:
    """Validate transition from current to target or raise IllegalTransition."""
    if allow_retry and current in RETRYABLE_SOURCE_STATES and target in RETRYABLE_TARGET_STATES:
        return target
    if not can_transition(current, target):
        raise IllegalTransition(current, target)
    return target
