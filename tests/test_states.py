"""Tests for state machine transitions (AC-1.1, INV-04, Section 4)."""

from itertools import product

import pytest

from agent_factory.domain.states import (
    ALLOWED_TRANSITIONS,
    IllegalTransition,
    ProjectState,
    can_transition,
    validate_transition,
)

ALL_STATES: list[ProjectState] = list(ProjectState)

VALID_TRANSITION_PAIRS: list[tuple[ProjectState, ProjectState]] = [
    (source, target)
    for source in ALL_STATES
    for target in ALL_STATES
    if target in ALLOWED_TRANSITIONS[source]
]

ILLEGAL_TRANSITION_PAIRS: list[tuple[ProjectState, ProjectState]] = [
    (source, target)
    for source, target in product(ALL_STATES, ALL_STATES)
    if target not in ALLOWED_TRANSITIONS[source]
]


@pytest.mark.parametrize(("source", "target"), VALID_TRANSITION_PAIRS)
def test_valid_transitions_succeed(source: ProjectState, target: ProjectState) -> None:
    """AC-1.1: Every valid transition in the state table succeeds and returns target."""
    assert can_transition(source, target) is True
    result = validate_transition(source, target)
    assert result == target


@pytest.mark.parametrize(("source", "target"), ILLEGAL_TRANSITION_PAIRS)
def test_illegal_transitions_raise_illegal_transition(
    source: ProjectState,
    target: ProjectState,
) -> None:
    """AC-1.1: Every illegal transition in the full state matrix raises IllegalTransition."""
    assert can_transition(source, target) is False
    with pytest.raises(IllegalTransition) as exc_info:
        validate_transition(source, target)
    assert exc_info.value.current == source
    assert exc_info.value.target == target
