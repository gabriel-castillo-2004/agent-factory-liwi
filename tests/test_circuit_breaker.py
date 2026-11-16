"""Tests for per-state call circuit breaker (AC-1.8, INV-17, F-06, F-15)."""

from pathlib import Path
from typing import Any

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


def _complete_spec_response() -> dict[str, Any]:
    return {
        "spec": {
            "objective": "Static unit converter",
            "users": ["Gabriel"],
            "features": ["Convert length"],
            "business_rules": ["Round to 4 decimals"],
            "edge_cases": ["Negative numbers"],
            "constraints": ["Zero backend"],
            "success_criteria": ["Instant conversion"],
            "assumptions": [],
        },
        "questions": [],
    }


def _pragmatist_dict() -> dict[str, Any]:
    return {
        "approach": "Pure client-side conversion functions",
        "components": ["converter.ts"],
        "tradeoffs": ["Static table"],
    }


def _structuralist_dict() -> dict[str, Any]:
    return {
        "critique": ["Floating point precision"],
        "robust_proposal": "Scaled integer math",
        "architectural_safeguards": ["Numeric parser"],
    }


def _plan_dict() -> dict[str, Any]:
    return {
        "summary": "Unit converter static app",
        "rationale": "Simple client-side architecture",
        "template_id": "static-app",
        "files": [
            {
                "path": "src/converter.ts",
                "purpose": "Conversion formulas",
                "contract": "Exports convert(v: number): number",
                "depends_on": [],
            }
        ],
        "acceptance_tests": [
            {
                "id": "AT-01",
                "description": "Converts meters to kilometers",
                "type": "unit",
            }
        ],
        "risks": ["None"],
        "deployment_target": "github-pages",
    }


def test_load_limits_reads_config_limits_toml() -> None:
    """AC-1.8: load_limits reads configured per-state limits from config/limits.toml."""
    limits = load_limits(Path("config/limits.toml"))
    assert limits["INTERVIEW"] == 6
    assert limits["DEBATE"] == 3
    assert limits["MEDIATION"] == 2
    assert limits["BUILDING"] == 15
    assert limits["ci_rounds"] == 3


def test_load_limits_raises_file_not_found_on_missing_config_file() -> None:
    """F-15: load_limits raises FileNotFoundError when the config file does not exist."""
    with pytest.raises(FileNotFoundError):
        load_limits(Path("nonexistent_dir/missing_limits.toml"))


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
    """AC-1.8 / M3: Exceeding the call budget of a state trips circuit breaker and transitions to FAILED."""
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


def test_run_debate_enforces_circuit_breaker_and_fails_when_retries_exceed_budget() -> None:
    """AC-1.8 / F-06 (INV-17): Debate retries cannot exceed DEBATE=3 call budget."""
    breaker = CircuitBreaker(config_path=Path("config/limits.toml"))
    store = MemoryEventStore()
    # 1 interview call + 2 invalid JSONs for Pragmatist + 1 valid Pragmatist (3 DEBATE calls)
    # + Structuralist call (4th DEBATE call -> must trip DEBATE=3 breaker before calling LLM!)
    fake_llm = FakeLLM(
        responses=[
            _complete_spec_response(),
            "<<<invalid-json-1>>>",
            "<<<invalid-json-2>>>",
            _pragmatist_dict(),
            _structuralist_dict(),
            _plan_dict(),
        ]
    )
    orchestrator = Orchestrator(store=store, llm=fake_llm, circuit_breaker=breaker)
    project_id = "proj-debate-cb-exceed"

    orchestrator.create_project(project_id=project_id, name="Debate CB", objective="Converter")
    orchestrator.handle_interview_message(project_id, "evt-int-1", "Complete converter spec")
    assert len(fake_llm.calls) == 1

    status = orchestrator.run_debate(project_id)

    assert status.state == ProjectState.FAILED
    assert status.failure_reason is not None
    assert "DEBATE" in status.failure_reason
    assert breaker.get_calls(project_id, ProjectState.DEBATE) == 3
    # 1 interview call + 3 debate calls (4th debate call blocked by central breaker)
    assert len(fake_llm.calls) == 4


def test_mediation_step_enforces_mediation_circuit_breaker_budget() -> None:
    """AC-1.8 / F-06 (INV-17): Mediator retries cannot exceed MEDIATION=2 call budget."""
    breaker = CircuitBreaker(config_path=Path("config/limits.toml"))
    store = MemoryEventStore()
    # 1 interview call + 2 DEBATE calls (Pragmatist + Structuralist)
    # + 2 invalid JSONs in MEDIATION (2 calls = limit) + 3rd MEDIATION attempt -> trips MEDIATION=2!
    fake_llm = FakeLLM(
        responses=[
            _complete_spec_response(),
            _pragmatist_dict(),
            _structuralist_dict(),
            "<<<bad-mediator-json-1>>>",
            "<<<bad-mediator-json-2>>>",
            _plan_dict(),
        ]
    )
    orchestrator = Orchestrator(store=store, llm=fake_llm, circuit_breaker=breaker)
    project_id = "proj-mediation-cb-exceed"

    orchestrator.create_project(project_id=project_id, name="Mediation CB", objective="Converter")
    orchestrator.handle_interview_message(project_id, "evt-int-1", "Complete converter spec")

    status = orchestrator.run_debate(project_id)

    assert status.state == ProjectState.FAILED
    assert status.failure_reason is not None
    assert "MEDIATION" in status.failure_reason
    assert breaker.get_calls(project_id, ProjectState.DEBATE) == 2
    assert breaker.get_calls(project_id, ProjectState.MEDIATION) == 2
    # 1 interview + 2 debate + 2 mediation = 5 total LLM calls (3rd mediation call blocked)
    assert len(fake_llm.calls) == 5


def test_circuit_breaker_never_resets_across_redebates_and_trips_on_cumulative_budget() -> None:
    """AC-1.8 / N-02 (INV-17): CircuitBreaker counters are cumulative per project and never reset on re-debate."""
    breaker = CircuitBreaker(config_path=Path("config/limits.toml"))
    store = MemoryEventStore()
    fake_llm = FakeLLM(
        responses=[
            _complete_spec_response(),
            # First run_debate: 2 DEBATE calls + 1 MEDIATION call -> WAITING_APPROVAL
            _pragmatist_dict(),
            _structuralist_dict(),
            _plan_dict(),
            # Second run_debate (adjustment): Pragmatist is 3rd cumulative DEBATE call,
            # Structuralist is 4th cumulative DEBATE call -> exceeds DEBATE=3!
            _pragmatist_dict(),
            _structuralist_dict(),
            _plan_dict(),
        ]
    )
    orchestrator = Orchestrator(store=store, llm=fake_llm, circuit_breaker=breaker)
    project_id = "proj-cumulative-cb"

    orchestrator.create_project(project_id=project_id, name="Cumulative CB", objective="Converter")
    orchestrator.handle_interview_message(project_id, "evt-int-1", "Complete converter spec")

    first_debate_status = orchestrator.run_debate(project_id)
    assert first_debate_status.state == ProjectState.WAITING_APPROVAL
    assert breaker.get_calls(project_id, ProjectState.DEBATE) == 2
    assert breaker.get_calls(project_id, ProjectState.MEDIATION) == 1

    second_debate_status = orchestrator.run_debate(project_id)
    assert second_debate_status.state == ProjectState.FAILED
    assert second_debate_status.failure_reason is not None
    assert "DEBATE" in second_debate_status.failure_reason
    assert breaker.get_calls(project_id, ProjectState.DEBATE) == 3
    # 1 interview + 3 first debate/mediation + 1 second debate pragmatist = 5 calls
    assert len(fake_llm.calls) == 5
