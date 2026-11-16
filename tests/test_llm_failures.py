"""Tests for LLM exception and timeout resilience across states (AC-1.13, INV-09, F-07)."""

from typing import Any

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.domain.events import EventType
from agent_factory.domain.states import ProjectState
from agent_factory.orchestrator import Orchestrator


def _complete_spec_response() -> dict[str, Any]:
    return {
        "spec": {
            "objective": "Static timer app",
            "users": ["Gabriel"],
            "features": ["Countdown"],
            "business_rules": ["Non-negative duration"],
            "edge_cases": ["Zero seconds"],
            "constraints": ["Static frontend"],
            "success_criteria": ["Accurate countdown"],
            "assumptions": [],
        },
        "questions": [],
    }


def test_llm_timeout_during_debate_records_failure_event_and_never_leaves_project_stuck() -> None:
    """AC-1.13 / F-07 (INV-09): TimeoutError during debate records PROJECT_FAILED and transitions to FAILED."""
    store = MemoryEventStore()
    fake_llm = FakeLLM(
        responses=[
            _complete_spec_response(),
            TimeoutError("LLM request timed out in Pragmatist"),
            TimeoutError("LLM request timed out in Pragmatist retry 1"),
            TimeoutError("LLM request timed out in Pragmatist retry 2"),
        ]
    )
    orchestrator = Orchestrator(store=store, llm=fake_llm)
    project_id = "proj-llm-timeout-debate"

    orchestrator.create_project(project_id=project_id, name="Timeout Debate", objective="Timer")
    orchestrator.handle_interview_message(project_id, "evt-int-1", "Complete timer spec")

    status = orchestrator.run_debate(project_id)

    assert status.state == ProjectState.FAILED
    assert status.failure_reason is not None
    assert "timed out" in status.failure_reason.lower()

    events = store.get_events(project_id)
    assert any(e.type == EventType.PROJECT_FAILED for e in events)


def test_llm_timeout_during_interview_records_failure_event_and_never_leaves_project_stuck() -> None:
    """AC-1.13 / F-07 (INV-09): TimeoutError during interview records PROJECT_FAILED and transitions to FAILED."""
    store = MemoryEventStore()
    fake_llm = FakeLLM(
        responses=[
            TimeoutError("LLM request timed out in Interviewer"),
        ]
    )
    orchestrator = Orchestrator(store=store, llm=fake_llm)
    project_id = "proj-llm-timeout-interview"

    orchestrator.create_project(project_id=project_id, name="Timeout Interview", objective="Timer")
    status = orchestrator.handle_interview_message(
        project_id=project_id,
        event_id="evt-int-timeout-1",
        user_message="Quiero un temporizador.",
    )

    assert status.state == ProjectState.FAILED
    assert status.failure_reason is not None
    assert "timed out" in status.failure_reason.lower()

    events = store.get_events(project_id)
    assert any(e.type == EventType.PROJECT_FAILED for e in events)
