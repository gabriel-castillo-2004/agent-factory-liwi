"""Tests for LLM exception and timeout resilience across states (AC-1.13, INV-09, F-07, N-01)."""

from typing import Any

import pytest

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.domain.events import EventType
from agent_factory.domain.states import ProjectState
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.llm import LLMError


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


@pytest.mark.parametrize(
    ("exc_cls", "expected_message"),
    [
        (ConnectionError, "LLM connection reset by peer"),
        (OSError, "LLM socket broken pipe"),
        (LLMError, "LLM adapter transport failure"),
    ],
)
def test_llm_transport_errors_during_interview_record_failure_event_and_never_leave_project_stuck(
    exc_cls: type[Exception],
    expected_message: str,
) -> None:
    """AC-1.13 / N-01 (INV-09): ConnectionError, OSError, and LLMError during interview transition to FAILED."""
    store = MemoryEventStore()
    fake_llm = FakeLLM(
        responses=[
            exc_cls(expected_message),
            exc_cls(expected_message),
            exc_cls(expected_message),
        ]
    )
    orchestrator = Orchestrator(store=store, llm=fake_llm)
    project_id = f"proj-int-{exc_cls.__name__.lower()}"

    orchestrator.create_project(project_id=project_id, name="Transport Fail Interview", objective="Timer")
    status = orchestrator.handle_interview_message(
        project_id=project_id,
        event_id="evt-int-transport-1",
        user_message="Quiero un temporizador.",
    )

    assert status.state == ProjectState.FAILED
    assert status.failure_reason is not None
    assert expected_message in status.failure_reason

    events = store.get_events(project_id)
    failed_events = [e for e in events if e.type == EventType.PROJECT_FAILED]
    assert len(failed_events) == 1
    assert expected_message in failed_events[0].payload["reason"]


@pytest.mark.parametrize(
    ("exc_cls", "expected_message"),
    [
        (ConnectionError, "LLM connection refused in debate"),
        (OSError, "LLM network unreachable in debate"),
        (LLMError, "LLM provider error in debate"),
    ],
)
def test_llm_transport_errors_during_debate_record_failure_event_and_never_leave_project_stuck(
    exc_cls: type[Exception],
    expected_message: str,
) -> None:
    """AC-1.13 / N-01 (INV-09): ConnectionError, OSError, and LLMError during debate transition to FAILED."""
    store = MemoryEventStore()
    fake_llm = FakeLLM(
        responses=[
            _complete_spec_response(),
            exc_cls(expected_message),
            exc_cls(expected_message),
            exc_cls(expected_message),
        ]
    )
    orchestrator = Orchestrator(store=store, llm=fake_llm)
    project_id = f"proj-deb-{exc_cls.__name__.lower()}"

    orchestrator.create_project(project_id=project_id, name="Transport Fail Debate", objective="Timer")
    orchestrator.handle_interview_message(project_id, "evt-int-1", "Complete timer spec")

    status = orchestrator.run_debate(project_id)

    assert status.state == ProjectState.FAILED
    assert status.failure_reason is not None
    assert expected_message in status.failure_reason

    events = store.get_events(project_id)
    failed_events = [e for e in events if e.type == EventType.PROJECT_FAILED]
    assert len(failed_events) == 1
    assert expected_message in failed_events[0].payload["reason"]
