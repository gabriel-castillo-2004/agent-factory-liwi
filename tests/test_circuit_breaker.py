"""Tests for per-state call circuit breaker (AC-1.8, INV-17, Section 7.8)."""

from pathlib import Path

import pytest

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.domain.states import ProjectState
from agent_factory.orchestrator import (
    CircuitBreaker,
    CircuitBreakerTrippedError,
    Orchestrator,
    load_limits,
)


def test_load_limits_reads_config_limits_toml() -> None:
    """AC-1.8: load_limits reads configured per-state limits from config/limits.toml."""
    limits = load_limits(Path("config/limits.toml"))
    assert limits["INTERVIEW"] == 6
    assert limits["DEBATE"] == 3
    assert limits["MEDIATION"] == 2
    assert limits["BUILDING"] == 15
    assert limits["ci_rounds"] == 3


@pytest.mark.parametrize(
    ("state", "expected_limit"),
    [
        (ProjectState.INTERVIEW, 6),
        (ProjectState.DEBATE, 3),
        (ProjectState.MEDIATION, 2),
        (ProjectState.BUILDING, 15),
    ],
)
def test_exceeding_state_call_budget_transitions_project_to_failed(
    state: ProjectState,
    expected_limit: int,
) -> None:
    """AC-1.8: Exceeding the call budget of a state trips circuit breaker and transitions to FAILED."""
    breaker = CircuitBreaker(config_path=Path("config/limits.toml"))
    assert breaker.get_limit(state) == expected_limit

    store = MemoryEventStore()
    fake_llm = FakeLLM()
    orchestrator = Orchestrator(
        store=store,
        llm=fake_llm,
        circuit_breaker=breaker,
    )
    project_id = f"proj-cb-{state.value.lower()}"
    orchestrator.create_project(project_id=project_id, name="CB Test", objective="Test limits")

    for call_num in range(1, expected_limit + 1):
        status = orchestrator.record_state_call(project_id=project_id, state=state)
        assert status.state != ProjectState.FAILED
        assert breaker.get_calls(project_id, state) == call_num

    failed_status = orchestrator.record_state_call(project_id=project_id, state=state)
    assert failed_status.state == ProjectState.FAILED
    assert failed_status.failure_reason is not None
    assert state.value in failed_status.failure_reason

    with pytest.raises(CircuitBreakerTrippedError):
        breaker.record_call(project_id, state)
